import asyncio
from types import SimpleNamespace

import pytest

from opensac import provider
from opensac.config import Settings
from opensac.errors import RuntimeClosedError
from opensac.provider import ProviderConfig, ProviderContext, load_provider
from opensac.runtime import Runtime


class CleanupProvider:
    def __init__(self):
        self.closed = 0

    async def search(self, query, limit):
        return []

    async def aclose(self):
        self.closed += 1


async def test_rejected_provider_is_closed_before_reporting_failure(monkeypatch):
    instance = CleanupProvider()
    instance.search = lambda query, limit: []
    created = []

    def factory(config, context):
        created.append(instance)
        return instance

    monkeypatch.setattr(
        provider, "entry_points", lambda **kwargs: [SimpleNamespace(load=lambda: factory)]
    )
    async with Runtime() as runtime:
        results = await runtime.search_many(["first", "second"])
        assert all(item.error.code == "configuration_error" for item in results)
        assert instance.closed == 1
        assert len(created) == 1
    assert instance.closed == 1


async def test_rejected_provider_cleanup_finishes_despite_cancellation(monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()

    class WrongProvider(CleanupProvider):
        search = None

        async def aclose(self):
            started.set()
            await release.wait()
            await super().aclose()

    instance = WrongProvider()
    monkeypatch.setattr(
        provider,
        "entry_points",
        lambda **kwargs: [SimpleNamespace(load=lambda: lambda config, context: instance)],
    )
    loading = asyncio.create_task(
        load_provider("search", "wrong", ProviderConfig(), ProviderContext())
    )
    await started.wait()
    loading.cancel()
    await asyncio.sleep(0)
    assert not loading.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await loading
    assert instance.closed == 1


async def test_cancelled_close_can_be_joined_without_repeating_cleanup():
    started, release = asyncio.Event(), asyncio.Event()

    class SlowClose(CleanupProvider):
        async def aclose(self):
            started.set()
            await release.wait()
            await super().aclose()

    first, second = SlowClose(), CleanupProvider()
    runtime = Runtime(search_provider=first, fetch_provider=first, rerank_provider=second)
    closing = asyncio.create_task(runtime.aclose())
    await started.wait()
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing
    with pytest.raises(RuntimeClosedError):
        await runtime.search("while closing")
    joining = asyncio.create_task(runtime.aclose())
    release.set()
    await joining
    await runtime.aclose()
    assert first.closed == second.closed == 1


async def test_provider_cancellation_during_cleanup_does_not_skip_other_providers():
    class CancelledClose(CleanupProvider):
        async def aclose(self):
            await super().aclose()
            raise asyncio.CancelledError

    first, second = CancelledClose(), CleanupProvider()
    runtime = Runtime(search_provider=first, fetch_provider=second)
    with pytest.raises(BaseExceptionGroup) as caught:
        await runtime.aclose()
    assert isinstance(caught.value.exceptions[0], asyncio.CancelledError)
    assert first.closed == second.closed == 1
    await runtime.aclose()


@pytest.mark.parametrize("failure", [ValueError, asyncio.CancelledError])
async def test_async_context_releases_resources_on_body_failure(failure):
    instance = CleanupProvider()
    runtime = Runtime(search_provider=instance)
    with pytest.raises(failure):
        async with runtime as active:
            assert active is runtime
            assert await active.search("query") == []
            raise failure()
    assert instance.closed == 1
    with pytest.raises(RuntimeClosedError):
        async with runtime:
            pass


async def test_runtime_configuration_is_a_detached_snapshot():
    settings = Settings(max_response_bytes=4096, search_options={"nested": {"value": 1}})
    async with Runtime(settings) as runtime:
        settings.max_response_bytes = 8192
        settings.search_options["nested"]["value"] = 2
        exposed = runtime.settings
        exposed.max_response_bytes = 16384
        exposed.search_options["nested"]["value"] = 3
        assert runtime.capabilities().limits["response_bytes"] == 4096
        assert runtime.context.max_response_bytes == 4096
        assert runtime.settings.search_options == {"nested": {"value": 1}}


async def test_concurrent_rejected_initialization_disposes_only_one_instance(monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()
    created = []

    class WrongProvider(CleanupProvider):
        search = None

        async def aclose(self):
            started.set()
            await release.wait()
            await super().aclose()

    def factory(config, context):
        instance = WrongProvider()
        created.append(instance)
        return instance

    monkeypatch.setattr(
        provider, "entry_points", lambda **kwargs: [SimpleNamespace(load=lambda: factory)]
    )
    async with Runtime() as runtime:
        batch = asyncio.create_task(runtime.search_many(["one", "two"]))
        await started.wait()
        await asyncio.sleep(0)
        assert len(created) == 1
        release.set()
        results = await batch
        assert all(item.error.code == "configuration_error" for item in results)
    assert created[0].closed == 1
