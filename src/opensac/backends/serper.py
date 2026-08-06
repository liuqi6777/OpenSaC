from __future__ import annotations

import asyncio
from urllib.parse import urlparse

import httpx
from opensac_sdk.models import ContentSnippet, SearchHit


class SerperBackend:
    name = "web"
    search_url = "https://google.serper.dev/search"
    scrape_url = "https://scrape.serper.dev"

    def __init__(self, api_key: str = "", timeout: float = 30.0) -> None:
        self.api_key = api_key
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            raise RuntimeError("Web search requires OPENSAC_SERPER_API_KEY")
        return {"X-API-KEY": self.api_key, "Content-Type": "application/json"}

    async def search(
        self,
        query: str,
        *,
        limit: int,
        domains: list[str] | None = None,
    ) -> list[SearchHit]:
        if domains:
            sites = " OR ".join(f"site:{domain}" for domain in domains)
            query = f"{query} ({sites})"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                self.search_url,
                headers=self._headers(),
                json={"q": query, "num": limit},
            )
            response.raise_for_status()
        payload = response.json()
        return [
            self._normalize_hit(hit, index + 1)
            for index, hit in enumerate(payload.get("organic", [])[:limit])
        ]

    def _normalize_hit(self, hit: dict, rank: int) -> SearchHit:
        url = str(hit.get("link", "") or "")
        return SearchHit(
            ref="",
            backend=self.name,
            title=str(hit.get("title", "") or ""),
            url=url,
            domain=urlparse(url).netloc or None,
            snippet=str(hit.get("snippet", "") or ""),
            rank=int(hit.get("position", rank)),
            metadata={k: v for k, v in hit.items() if k not in {"title", "link", "snippet"}},
        )

    async def content(
        self,
        hits: list[SearchHit],
        *,
        query: str | None = None,
    ) -> list[ContentSnippet]:
        del query
        urls = [hit for hit in hits if hit.url]
        if not urls:
            return []
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            results = await asyncio.gather(
                *(self._scrape(client, hit) for hit in urls),
                return_exceptions=True,
            )
        return [snippet for snippet in results if isinstance(snippet, ContentSnippet)]

    async def _scrape(self, client: httpx.AsyncClient, hit: SearchHit) -> ContentSnippet:
        response = await client.post(
            self.scrape_url,
            headers=self._headers(),
            json={"url": hit.url},
        )
        response.raise_for_status()
        payload = response.json()
        return ContentSnippet(
            ref=hit.ref,
            text=str(payload.get("text", "") or payload.get("markdown", "")),
            url=hit.url,
            title=hit.title,
            metadata={"backend": self.name},
        )
