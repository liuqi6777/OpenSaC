import asyncio

import httpx
import pytest
from conftest import FakeProvider
from pydantic import SecretStr

from opensac.config import Settings
from opensac.errors import CapabilityError
from opensac.provider import ProviderConfig, ProviderContext
from opensac.providers.jina import JinaFetch
from opensac.providers.serper import SerperSearch
from opensac.runtime import Runtime


@pytest.mark.asyncio
@pytest.mark.parametrize("date", [None, "2026-09-07", "2 days ago"])
async def test_provider_wire_and_normalization(date) -> None:
    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "google.serper.dev":
            return httpx.Response(
                200,
                json={
                    "organic": [
                        {
                            "link": "https://example.com/paper",
                            "title": "Title",
                            "snippet": "Summary",
                            "date": date,
                        }
                    ]
                },
            )
        return httpx.Response(200, text="# Full paper")

    search = SerperSearch(
        ProviderConfig(api_key=SecretStr("search-secret")),
        ProviderContext(),
        transport=httpx.MockTransport(handle),
    )
    fetch = JinaFetch(ProviderConfig(), ProviderContext(), transport=httpx.MockTransport(handle))
    try:
        hits = await search.search("query", 5)
        document = await fetch.fetch(hits[0].url)
        assert document.text == "# Full paper"
        assert hits[0].domain == "example.com"
        assert hits[0].date == date
        assert requests[0].headers["X-API-KEY"] == "search-secret"
        assert requests[1].url.host == "r.jina.ai"
        assert requests[1].url.path == "/https://example.com/paper"
    finally:
        await search.aclose()
        await fetch.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,body,code",
    [
        (429, b"secret", "provider_rate_limited"),
        (500, b"secret", "provider_error"),
        (302, b"", "provider_error"),
        (200, b"x" * 1025, "response_too_large"),
        (200, b"", "provider_invalid_response"),
    ],
)
async def test_provider_failure_and_size_bound(status, body, code) -> None:
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(status, content=body, headers={"Location": "http://127.0.0.1"})

    provider = JinaFetch(
        ProviderConfig(),
        ProviderContext(max_response_bytes=1024),
        transport=httpx.MockTransport(handle),
    )
    try:
        with pytest.raises(CapabilityError) as caught:
            await provider.fetch("https://example.com")
        assert caught.value.code == code
        assert "secret" not in caught.value.message
        assert len(calls) == 1
    finally:
        await provider.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body", [{}, {"organic": {}}, {"organic": [None]}, {"organic": [{"link": 1}]}]
)
async def test_bad_search_payload(body) -> None:
    provider = SerperSearch(
        ProviderConfig(api_key=SecretStr("search")),
        ProviderContext(),
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)),
    )
    try:
        with pytest.raises(CapabilityError) as caught:
            await provider.search("q", 5)
        assert caught.value.code == "provider_invalid_response"
    finally:
        await provider.aclose()


@pytest.mark.asyncio
async def test_timeout_releases_capacity_and_closes_provider() -> None:
    class SlowProvider(FakeProvider):
        async def fetch(self, url):
            if url.endswith("slow"):
                await asyncio.sleep(1)
            return await super().fetch(url)

    provider = SlowProvider()
    settings = Settings(request_timeout=0.05, max_concurrency=1)
    runtime = Runtime(settings, fetch_provider=provider)
    try:
        with pytest.raises(CapabilityError) as caught:
            await runtime.fetch("https://example.com/slow")
        assert caught.value.code == "request_timeout"
        assert (await runtime.fetch("https://example.com/fast")).text == "Full document"
    finally:
        await runtime.aclose()
    assert provider.closed


@pytest.mark.asyncio
async def test_waiting_for_capacity_does_not_consume_request_timeout() -> None:
    class SlowProvider(FakeProvider):
        async def fetch(self, url):
            await asyncio.sleep(0.2)
            return await super().fetch(url)

    runtime = Runtime(
        Settings(request_timeout=0.3, max_concurrency=1), fetch_provider=SlowProvider()
    )
    try:
        results = await runtime.fetch_many(
            ["https://example.com/first", "https://example.com/queued"]
        )
        assert all(item.data is not None for item in results)
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_provider_concurrency_is_bounded() -> None:
    class CountingProvider(FakeProvider):
        active = 0
        peak = 0

        async def fetch(self, url):
            self.active += 1
            self.peak = max(self.peak, self.active)
            try:
                await asyncio.sleep(0.01)
                return await super().fetch(url)
            finally:
                self.active -= 1

    provider = CountingProvider()
    runtime = Runtime(Settings(max_concurrency=2), fetch_provider=provider)
    try:
        results = await runtime.fetch_many([f"https://example.com/{i}" for i in range(8)])
        assert provider.peak == 2
        assert len(results) == 8
        assert all(item.data is not None for item in results)
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 422])
async def test_jina_decides_target_acceptance(status):
    url = "http://localhost:9000/paper"
    calls = []

    def handle(request):
        calls.append(request)
        assert request.url.host == "r.jina.ai"
        assert request.url.path == f"/{url}"
        return httpx.Response(status, text="Reader response")

    fetch = JinaFetch(ProviderConfig(), ProviderContext(), transport=httpx.MockTransport(handle))
    try:
        if status == 200:
            assert (await fetch.fetch(url)).url == url
        else:
            with pytest.raises(CapabilityError) as caught:
                await fetch.fetch(url)
            assert caught.value.code == "provider_error"
        assert len(calls) == 1
    finally:
        await fetch.aclose()
