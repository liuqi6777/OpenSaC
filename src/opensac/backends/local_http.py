from __future__ import annotations

import asyncio
from urllib.parse import urljoin

import httpx
from opensac_sdk.models import ContentSnippet, SearchHit


class LocalSearchBackend:
    name = "local"

    def __init__(self, base_url: str, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        self.timeout = timeout

    async def search(
        self,
        query: str,
        *,
        limit: int,
        domains: list[str] | None = None,
    ) -> list[SearchHit]:
        del domains
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                urljoin(self.base_url, "search"),
                json={"query": query, "top_k": limit},
            )
            response.raise_for_status()
        payload = response.json()
        rows = payload.get("results", [{}])
        hits = rows[0].get("hits", []) if rows else []
        return [
            SearchHit(
                ref="",
                backend=self.name,
                docid=str(hit["docid"]),
                snippet=str(hit.get("snippet", "")),
                score=hit.get("score"),
                rank=int(hit.get("rank", index + 1)),
            )
            for index, hit in enumerate(hits)
        ]

    async def content(
        self,
        hits: list[SearchHit],
        *,
        query: str | None = None,
    ) -> list[ContentSnippet]:
        del query

        async def fetch(client: httpx.AsyncClient, hit: SearchHit) -> ContentSnippet:
            response = await client.post(
                urljoin(self.base_url, "get_document"),
                json={"docid": hit.docid},
            )
            response.raise_for_status()
            payload = response.json()
            return ContentSnippet(
                ref=hit.ref,
                text=str(payload.get("text", "")),
                title=hit.title,
                metadata={"docid": hit.docid, "backend": self.name},
            )

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            return await asyncio.gather(*(fetch(client, hit) for hit in hits))
