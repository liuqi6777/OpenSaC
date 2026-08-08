from __future__ import annotations

import asyncio
from urllib.parse import urlparse

import httpx
from opensac_sdk.models import ContentSnippet, SearchHit


class SerperBackend:
    name = "web"
    search_url = "https://google.serper.dev/search"
    scrape_url = "https://scrape.serper.dev"
    # Pushed down into the query as `site:` rather than filtered afterwards,
    # which is the point: the ranking is recomputed under the constraint instead
    # of being thinned after the fact.
    supports_domains = True
    # Results one SERP request will serve. Depth past this is not a matter of
    # asking harder -- there is no such response -- so it is refused rather
    # than quietly clipped. A program told it read rank 150 when it read rank
    # 100 draws exactly the wrong conclusion about why it found nothing.
    max_depth = 100

    def __init__(
        self,
        api_key: str = "",
        timeout: float = 30.0,
        fetch_concurrency: int = 6,
    ) -> None:
        self.api_key = api_key
        self.timeout = timeout
        # Scraping is a metered, rate-limited API and one call here can carry
        # the whole candidate pool. The broker's semaphore admits this call as
        # a single unit and cannot see inside it.
        self._fetch_gate = asyncio.Semaphore(max(1, fetch_concurrency))

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            raise RuntimeError("Web search requires OPENSAC_SERPER_API_KEY")
        return {"X-API-KEY": self.api_key, "Content-Type": "application/json"}

    async def search(
        self,
        query: str,
        *,
        limit: int,
        offset: int = 0,
        domains: list[str] | None = None,
    ) -> list[SearchHit]:
        if domains:
            sites = " OR ".join(f"site:{domain}" for domain in domains)
            query = f"{query} ({sites})"
        # Deepen the request and slice, rather than using Serper's `page`:
        # paging renumbers `position` per page, and the rank a hit carries has
        # to stay comparable across the two backends and joinable offline.
        # `max_depth` is declared, not checked here: the broker refuses a
        # request past it before this runs, so that every backend's ceiling is
        # reported to the program in the same words.
        depth = offset + limit
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                self.search_url,
                headers=self._headers(),
                json={"q": query, "num": depth},
            )
            response.raise_for_status()
        payload = response.json()
        return [
            self._normalize_hit(hit, index + 1)
            for index, hit in enumerate(payload.get("organic", [])[:depth])
            if index >= offset
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
        if not hits:
            return []
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            return list(await asyncio.gather(*(self._scrape(client, hit) for hit in hits)))

    def _failed(self, hit: SearchHit, reason: str) -> ContentSnippet:
        return ContentSnippet(
            ref=hit.ref,
            text="",
            url=hit.url,
            title=hit.title,
            metadata={"backend": self.name, "fetch_error": reason},
        )

    async def _scrape(self, client: httpx.AsyncClient, hit: SearchHit) -> ContentSnippet:
        if not hit.url:
            return self._failed(hit, "hit carries no URL to scrape")
        try:
            async with self._fetch_gate:
                response = await client.post(
                    self.scrape_url,
                    headers=self._headers(),
                    json={"url": hit.url},
                )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            # A page behind a paywall, a robots block, or a timeout is ordinary
            # on the open web and must not fail the batch. It is reported as
            # itself so a program can tell "nobody could read this" apart from
            # "this said nothing", which decides whether re-querying is worth
            # anything.
            return self._failed(hit, f"{type(exc).__name__}: {exc}")
        return ContentSnippet(
            ref=hit.ref,
            text=str(payload.get("text", "") or payload.get("markdown", "")),
            url=hit.url,
            title=hit.title,
            metadata={"backend": self.name},
        )
