import json
from types import SimpleNamespace

import httpx
import pytest
from pydantic import HttpUrl, SecretStr

from opensac import Client, provider
from opensac.config import Settings
from opensac.contracts import Document, SearchHit
from opensac.provider import ProviderConfig, ProviderContext, load_provider
from opensac.providers.http import HTTPFetch, HTTPSearch
from opensac.runtime import Runtime


class ExampleSearch:
    """A search-only provider: deliberately has no fetch method."""

    def __init__(self, config, context):
        self.config = config
        self.closed = False

    async def search(self, query, limit):
        return [SearchHit(url="http://localhost:9000/paper", title=query, snippet="Summary")]

    async def aclose(self):
        self.closed = True


class ExampleFetch:
    """An independently installed fetch provider: deliberately has no search method."""

    def __init__(self, config, context):
        self.config = config
        self.closed = False

    async def fetch(self, url):
        return Document(url=url, text="Paper body")

    async def aclose(self):
        self.closed = True


def test_external_capability_providers_loaded_by_entry_points(monkeypatch) -> None:
    requested = []
    instances = []

    def discover(*, group, name):
        requested.append((group, name))
        types = {
            ("opensac.search", "custom-search"): ExampleSearch,
            ("opensac.fetch", "custom-fetch"): ExampleFetch,
        }

        def factory(config, context):
            instance = types[group, name](config, context)
            instances.append(instance)
            return instance

        return [SimpleNamespace(load=lambda: factory)]

    monkeypatch.setattr(provider, "entry_points", discover)
    config = Settings(
        search_provider="custom-search",
        search_api_key=SecretStr("search-only"),
        fetch_provider="custom-fetch",
        fetch_api_key=SecretStr("fetch-only"),
    )
    with Client(settings=config) as sdk:
        hits = sdk.search("query")
        document = sdk.content.fetch(hits[0].url)
        assert document.url == "http://localhost:9000/paper"
        assert document.text == "Paper body"
        assert instances[0].config.api_key.get_secret_value() == "search-only"
        assert instances[1].config.api_key.get_secret_value() == "fetch-only"
    assert all(instance.closed for instance in instances)
    assert requested == [("opensac.search", "custom-search"), ("opensac.fetch", "custom-fetch")]


@pytest.mark.asyncio
async def test_installed_entry_points_resolve_independently() -> None:
    search = load_provider("search", "serper", ProviderConfig(), ProviderContext())
    fetch = load_provider("fetch", "jina", ProviderConfig(), ProviderContext())
    try:
        assert callable(search.search)
        assert callable(fetch.fetch)
        assert not hasattr(search, "fetch")
        assert not hasattr(fetch, "search")
    finally:
        await search.aclose()
        await fetch.aclose()


@pytest.mark.parametrize("count", [0, 2])
def test_missing_or_ambiguous_provider_fails_at_startup(monkeypatch, count):
    monkeypatch.setattr(provider, "entry_points", lambda **kwargs: [object()] * count)
    with pytest.raises(ValueError):
        load_provider("search", "ambiguous", ProviderConfig(), ProviderContext())


def test_wrong_capability_provider_is_rejected(monkeypatch):
    monkeypatch.setattr(
        provider, "entry_points", lambda **kwargs: [SimpleNamespace(load=lambda: ExampleFetch)]
    )
    with pytest.raises(TypeError):
        load_provider("search", "wrong", ProviderConfig(), ProviderContext())


@pytest.mark.asyncio
async def test_http_providers_use_independent_endpoints_and_preserve_native_url(tmp_path) -> None:
    document_url = "http://localhost:9000/papers/article.html"
    calls = []

    def handle(request):
        calls.append(request)
        if request.url.host == "search-service":
            assert request.url.path == "/v1/search"
            assert json.loads(request.content) == {"query": "research", "limit": 5}
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "url": document_url,
                            "title": "Paper",
                            "snippet": "Summary",
                            "domain": "collection.example",
                            "date": "yesterday",
                        }
                    ]
                },
            )
        assert request.url.host == "fetch-service"
        assert request.url.path == "/v1/fetch"
        assert json.loads(request.content) == {"url": document_url}
        return httpx.Response(
            200,
            json={
                "data": {
                    "url": document_url,
                    "text": "Native document body",
                }
            },
        )

    search = HTTPSearch(
        ProviderConfig(base_url=HttpUrl("http://search-service/v1")),
        ProviderContext(),
        transport=httpx.MockTransport(handle),
    )
    fetch = HTTPFetch(
        ProviderConfig(base_url=HttpUrl("http://fetch-service/v1")),
        ProviderContext(),
        transport=httpx.MockTransport(handle),
    )
    runtime = Runtime(search_provider=search, fetch_provider=fetch)
    try:
        document = await runtime.fetch(document_url)
        assert document.url == document_url
        hit = (await runtime.search("research"))[0]
        assert hit.url == document_url
        assert hit.domain == "collection.example"
        assert hit.date == "yesterday"
        (tmp_path / "document.json").write_text(document.model_dump_json())
    finally:
        await runtime.aclose()
    assert len(calls) == 2
