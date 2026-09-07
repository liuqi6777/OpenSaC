import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

from opensac import Client, provider
from opensac.client import LazySDK
from opensac.config import Settings
from opensac.contracts import Document, SearchHit
from opensac.errors import CapabilityError, OpenSACError
from opensac.runtime import Runtime


class BoundSearch:
    def __init__(self, config, context):
        self.loop = asyncio.get_running_loop()
        self.closed = False

    async def search(self, query, limit):
        assert asyncio.get_running_loop() is self.loop
        return [SearchHit(url="http://localhost:9000/paper", title=query, snippet="Evidence")]

    async def aclose(self):
        assert asyncio.get_running_loop() is self.loop
        self.closed = True


class BoundFetch:
    def __init__(self, config, context):
        self.loop = asyncio.get_running_loop()
        self.closed = False
        self.active = 0
        self.peak = 0

    async def fetch(self, url):
        assert asyncio.get_running_loop() is self.loop
        if url.endswith("/failed"):
            raise CapabilityError("provider_timeout", "Timed out.", 504, True)
        self.active += 1
        self.peak = max(self.active, self.peak)
        try:
            await asyncio.sleep(0.01)
            return Document(url=url, text="Body")
        finally:
            self.active -= 1

    async def aclose(self):
        assert asyncio.get_running_loop() is self.loop
        self.closed = True


@pytest.fixture
def installed_providers(monkeypatch):
    instances = []

    def discover(*, group, name):
        def factory(config, context):
            implementation = {"opensac.search": BoundSearch, "opensac.fetch": BoundFetch}[group]
            instance = implementation(config, context)
            instances.append(instance)
            return instance

        return [SimpleNamespace(load=lambda: factory)]

    monkeypatch.setattr(provider, "entry_points", discover)
    return instances


def test_sdk_initializes_lazily_and_reuses_resources(installed_providers, tmp_path):
    with Client(settings=Settings(max_concurrency=2)) as sdk:
        assert installed_providers == []
        assert "search" in sdk.capabilities().methods
        assert installed_providers == []
        for query in ("first", "second"):
            hit = sdk.search(query)[0]
            document = sdk.content.fetch(hit.url)
            assert document.url == hit.url
        result = sdk.content.fetch_many(["http://localhost:9000/paper"] * 6)
        assert len(result) == 6
        (tmp_path / "document.json").write_text(document.model_dump_json())
        assert json.loads((tmp_path / "document.json").read_text())["text"] == "Body"
        assert len(installed_providers) == 2
        assert installed_providers[1].peak == 2
    assert all(instance.closed for instance in installed_providers)
    assert not any(thread.name == "opensac-runtime" for thread in threading.enumerate())
    with pytest.raises(OpenSACError) as caught:
        sdk.search("after close")
    assert caught.value.code == "client_closed"


@pytest.mark.asyncio
async def test_sync_sdk_works_inside_an_existing_event_loop(installed_providers):
    caller_loop = asyncio.get_running_loop()
    with Client() as sdk:
        assert sdk.search("notebook")[0].title == "notebook"
        assert installed_providers[0].loop is not caller_loop


def test_fetch_does_not_initialize_a_search_provider(installed_providers):
    with Client() as sdk:
        sdk.content.fetch("http://localhost:9000/paper")
        assert len(installed_providers) == 1
        assert isinstance(installed_providers[0], BoundFetch)


def test_provider_failures_are_structured(installed_providers):
    with Client() as sdk:
        with pytest.raises(OpenSACError) as caught:
            sdk.content.fetch("http://localhost:9000/failed")
        assert caught.value.code == "provider_timeout"
        assert caught.value.status_code == 504
        assert caught.value.retryable
        results = sdk.content.fetch_many(
            [
                "http://localhost:9000/ok",
                "http://localhost:9000/failed",
            ]
        )
        assert results[0].data.text == "Body"
        assert results[1].error.code == "provider_timeout"
        with pytest.raises(OpenSACError) as invalid:
            sdk.content.fetch("opaque-reference")
        assert invalid.value.code == "invalid_request"


def test_singleton_close_allows_fresh_direct_configuration(installed_providers):
    sdk = LazySDK()
    try:
        sdk.search("one")
        sdk.close()
        sdk.search("two")
        assert len(installed_providers) == 2
        assert installed_providers[0].closed
        assert not installed_providers[1].closed
    finally:
        sdk.close()


@pytest.mark.asyncio
async def test_async_runtime_can_be_used_without_sync_bridge(installed_providers):
    runtime = Runtime()
    try:
        hits = await runtime.search("async")
        assert (await runtime.fetch(hits[0].url)).text == "Body"
        assert installed_providers[0].loop is asyncio.get_running_loop()
    finally:
        await runtime.aclose()
    with pytest.raises(CapabilityError) as caught:
        await runtime.search("closed")
    assert caught.value.code == "runtime_closed"


def test_lazy_config_is_read_at_first_use(monkeypatch):
    sdk = LazySDK()
    monkeypatch.setenv("OPENSAC_MAX_RESPONSE_BYTES", "4096")
    try:
        assert sdk.capabilities().limits["response_bytes"] == 4096
        monkeypatch.setenv("OPENSAC_MAX_RESPONSE_BYTES", "8192")
        assert sdk.capabilities().limits["response_bytes"] == 4096
        sdk.close()
        assert sdk.capabilities().limits["response_bytes"] == 8192
    finally:
        sdk.close()


@pytest.mark.asyncio
async def test_invalid_batch_arguments_fail_before_any_provider_call(installed_providers):
    runtime = Runtime()
    try:
        for operation in [
            lambda: runtime.search([]),
            lambda: runtime.search(["query"] * 11),
            lambda: runtime.search_many(["valid", " "]),
            lambda: runtime.fetch_many(["https://example.com", "invalid"]),
            lambda: runtime.complete_many(["valid", ""]),
            lambda: runtime.complete_many(["valid"], max_tokens=True),
            lambda: runtime.extract_many(["valid", ""], {"type": "string"}),
            lambda: runtime.extract_many(["valid"], {"type": "string"}, temperature=3),
        ]:
            with pytest.raises(CapabilityError) as caught:
                await operation()
            assert caught.value.code == "invalid_request"
        assert installed_providers == []
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_search_reformulations_fuse_rankings_and_surface_failures():
    class VariantSearch:
        async def search(self, query, limit):
            if query == "failed":
                raise CapabilityError("provider_timeout", "Timed out.", 504, True)
            urls = {
                "primary": ["a", "shared"],
                "alternate": ["shared", "b"],
            }[query]
            return [
                SearchHit(url=f"https://example.com/{url}", title=url, snippet="Evidence")
                for url in urls[:limit]
            ]

        async def aclose(self):
            pass

    runtime = Runtime(search_provider=VariantSearch())
    try:
        hits = await runtime.search(["primary", "alternate"], limit=2)
        assert [hit.title for hit in hits] == ["shared", "a"]
        with pytest.raises(CapabilityError) as caught:
            await runtime.search(["primary", "failed"])
        assert caught.value.code == "provider_timeout"
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_search_reformulation_failure_cancels_unfinished_requests():
    slow_started = asyncio.Event()
    slow_cancelled = asyncio.Event()

    class VariantSearch:
        async def search(self, query, limit):
            if query == "failed":
                await slow_started.wait()
                raise CapabilityError("provider_timeout", "Timed out.", 504, True)
            slow_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                slow_cancelled.set()

        async def aclose(self):
            pass

    runtime = Runtime(search_provider=VariantSearch())
    try:
        with pytest.raises(CapabilityError) as caught:
            await runtime.search(["failed", "slow"])
        assert caught.value.code == "provider_timeout"
        assert slow_cancelled.is_set()
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_runtime_model_methods_accept_ordinary_arguments():
    from opensac import Completion

    class Model:
        async def complete(self, prompt, *, schema=None):
            return Completion(text='{"count": 2}' if schema else "answer", model="test")

        async def aclose(self):
            pass

    runtime = Runtime(llm_provider=Model())
    try:
        assert (await runtime.complete("hello")).text == "answer"
        schema = {"type": "object", "properties": {"count": {"type": "integer"}}}
        assert (await runtime.extract("two", schema)).data == {"count": 2}
        assert (await runtime.complete_many(["hello"]))[0].data.text == "answer"
        assert (await runtime.extract_many(["two"], schema))[0].data.data == {"count": 2}
    finally:
        await runtime.aclose()
