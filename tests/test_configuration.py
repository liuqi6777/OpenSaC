import json
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from opensac import Client, provider
from opensac.config import Settings
from opensac.errors import ConfigurationError
from opensac.provider import ProviderConfig, ProviderContext
from opensac.providers.http import HTTPFetch, HTTPRerank, HTTPSearch
from opensac.providers.jina import JinaFetch
from opensac.providers.openai import OpenAILLM
from opensac.providers.serper import SerperSearch


def test_client_snapshots_explicit_settings_before_first_use(monkeypatch):
    captured = []

    class Search:
        def __init__(self, config, context):
            captured.append((config, context))

        async def search(self, query, limit):
            return []

        async def aclose(self):
            pass

    monkeypatch.setattr(
        provider, "entry_points", lambda **kwargs: [SimpleNamespace(load=lambda: Search)]
    )
    settings = Settings(max_response_bytes=4096, search_options={"nested": {"value": 1}})
    with Client(settings=settings) as client:
        settings.max_response_bytes = 8192
        settings.search_options["nested"]["value"] = 2
        assert client.search("query") == []
        assert client.capabilities().limits["response_bytes"] == 4096
    assert captured[0][0].options == {"nested": {"value": 1}}
    assert captured[0][1].max_response_bytes == 4096


async def test_serper_options_are_transmitted_and_config_is_copied():
    requests = []

    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"organic": []})

    config = ProviderConfig(api_key=SecretStr("test"), options={"gl": "us", "hl": "en"})
    search = SerperSearch(config, ProviderContext(), transport=httpx.MockTransport(handle))
    config.options["gl"] = "de"
    try:
        assert await search.search("query", 5) == []
    finally:
        await search.aclose()
    assert requests == [{"q": "query", "num": 5, "gl": "us", "hl": "en"}]


@pytest.mark.parametrize(
    "implementation,options",
    [
        (SerperSearch, {"q": "override"}),
        (SerperSearch, {"num": 1}),
        (SerperSearch, {"unknown": True}),
        (SerperSearch, {"gl": 1}),
        (SerperSearch, {"hl": " "}),
        (JinaFetch, {"ignored": True}),
        (HTTPSearch, {"limit": 1}),
        (HTTPFetch, {"url": "override"}),
        (HTTPRerank, {"top_n": 1}),
        (OpenAILLM, {"model": "override"}),
        (HTTPSearch, {"value": float("nan")}),
    ],
)
def test_invalid_options_fail_before_allocating_http_resources(
    implementation, options, monkeypatch
):
    def forbidden(*args, **kwargs):
        raise AssertionError("Invalid options must fail before allocating a client")

    monkeypatch.setattr(httpx, "AsyncClient", forbidden)
    with pytest.raises(ConfigurationError):
        implementation(ProviderConfig(options=options), ProviderContext())


@pytest.mark.parametrize("implementation", [HTTPSearch, HTTPFetch, HTTPRerank])
async def test_http_options_reach_backend(implementation):
    payloads = []

    def handle(request):
        payload = json.loads(request.content)
        payloads.append(payload)
        if implementation is HTTPSearch:
            return httpx.Response(200, json={"data": []})
        if implementation is HTTPFetch:
            return httpx.Response(200, json={"data": {"url": payload["url"], "text": "body"}})
        return httpx.Response(200, json={"results": [{"index": 0, "relevance_score": 1}]})

    instance = implementation(
        ProviderConfig(
            base_url="https://example.com", model="ranker", options={"collection": "papers"}
        ),
        ProviderContext(),
        transport=httpx.MockTransport(handle),
    )
    try:
        if implementation is HTTPSearch:
            await instance.search("query", 5)
        elif implementation is HTTPFetch:
            await instance.fetch("https://example.com/paper")
        else:
            await instance.rerank("query", ["text"], top_n=1)
    finally:
        await instance.aclose()
    assert payloads[0]["collection"] == "papers"


def test_configuration_checks_are_offline_and_do_not_initialize_providers(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Configuration checks must not allocate HTTP clients")

    monkeypatch.setattr(httpx, "AsyncClient", forbidden)
    with Client(settings=Settings(search_api_key="", llm_model=None, llm_base_url=None)) as client:
        checks = {item.capability: item for item in client.check_configuration()}
    assert checks["search"].status == "failed"
    assert checks["search"].error.code == "not_configured"
    assert checks["fetch"].status == "passed"
    assert checks["llm"].error.code == "not_configured"


def test_diagnostics_do_not_claim_validation_of_unknown_factories(monkeypatch):
    def factory(config, context):
        raise AssertionError("Diagnostics must not construct providers")

    monkeypatch.setattr(
        provider, "entry_points", lambda **kwargs: [SimpleNamespace(load=lambda: factory)]
    )
    with Client() as client:
        assert all(item.status == "unverified" for item in client.check_configuration())


@pytest.mark.parametrize("mode", ["missing", "duplicate", "broken_import"])
def test_diagnostics_report_discovery_failures_without_private_exception_text(mode, monkeypatch):
    def load():
        raise RuntimeError("private-token-123")

    entries = (
        []
        if mode == "missing"
        else [SimpleNamespace(load=load)] * (2 if mode == "duplicate" else 1)
    )
    monkeypatch.setattr(provider, "entry_points", lambda **kwargs: entries)
    with Client() as client:
        checks = client.check_configuration()
    assert all(item.status == "failed" for item in checks)
    assert all(item.error.code == "configuration_error" for item in checks)
    assert all("private-token-123" not in item.model_dump_json() for item in checks)


def test_custom_validator_does_not_mutate_runtime_configuration():
    from unittest.mock import patch

    from opensac.runtime import Runtime

    class Custom:
        def __init__(self, config, context):
            raise AssertionError("Diagnostics must not initialize providers")

        @staticmethod
        def validate_config(config):
            if "nested" in config.options:
                config.options["nested"]["value"] = "changed"

    with patch(
        "opensac.provider.entry_points", return_value=[SimpleNamespace(load=lambda: Custom)]
    ):
        runtime = Runtime(Settings(search_options={"nested": {"value": "original"}}))
        checks = runtime.check_configuration()
    assert all(item.status == "passed" for item in checks)
    assert runtime.settings.search_options == {"nested": {"value": "original"}}


@pytest.mark.parametrize("mode", ["async_factory", "async_validator", "returned_coroutine"])
def test_diagnostics_reject_async_hooks_without_unawaited_coroutines(mode, monkeypatch):
    async def asynchronous(*args):
        raise AssertionError("Asynchronous hooks must not execute")

    class Custom:
        validate_config = staticmethod(
            (lambda config: asynchronous(config)) if mode == "returned_coroutine" else asynchronous
        )

    factory = asynchronous if mode == "async_factory" else Custom
    monkeypatch.setattr(
        provider, "entry_points", lambda **kwargs: [SimpleNamespace(load=lambda: factory)]
    )
    with Client() as client:
        checks = client.check_configuration()
    assert all(item.status == "failed" for item in checks)
    assert all(item.error.code == "configuration_error" for item in checks)


async def test_diagnostics_skip_settings_for_injected_instances(monkeypatch):
    from opensac.runtime import Runtime

    class Injected:
        async def search(self, query, limit):
            return []

        async def aclose(self):
            pass

    def discover(*, group, name):
        assert group != "opensac.search"
        return []

    monkeypatch.setattr(provider, "entry_points", discover)
    async with Runtime(
        Settings(search_provider="not-installed"), search_provider=Injected()
    ) as runtime:
        checks = {item.capability: item for item in runtime.check_configuration()}
        assert checks["search"].status == "unverified"
        assert checks["search"].provider == "injected"
        assert checks["search"].error is None
        assert await runtime.search("query") == []
