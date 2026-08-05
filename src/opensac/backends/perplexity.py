from __future__ import annotations

import asyncio
import os
from typing import Any
from urllib.parse import urlparse

from opensac_sdk.models import ContentSnippet, SearchHit


class PerplexityBackend:
    name = "web"

    def __init__(self, api_key: str = "") -> None:
        if api_key:
            os.environ.setdefault("PERPLEXITY_API_KEY", api_key)

    @staticmethod
    def _module():
        try:
            import pplx_sdk
        except ImportError as exc:
            raise RuntimeError(
                "Web search requires the optional perplexity-sdk dependency"
            ) from exc
        return pplx_sdk

    async def search(
        self,
        query: str,
        *,
        limit: int,
        domains: list[str] | None = None,
    ) -> list[SearchHit]:
        module = self._module()
        raw_hits = await asyncio.to_thread(
            module.search.web,
            query,
            limit=limit,
            domains=domains,
        )
        return [self._normalize_hit(hit, index + 1) for index, hit in enumerate(raw_hits)]

    def _normalize_hit(self, hit: Any, rank: int) -> SearchHit:
        url = str(getattr(hit, "url", "") or "")
        metadata = hit.model_dump() if hasattr(hit, "model_dump") else {}
        return SearchHit(
            ref="",
            backend=self.name,
            title=str(getattr(hit, "title", "") or ""),
            url=url,
            domain=urlparse(url).netloc or None,
            snippet=str(
                getattr(hit, "snippet", "")
                or getattr(hit, "text", "")
                or getattr(hit, "description", "")
            ),
            score=getattr(hit, "score", None),
            rank=rank,
            metadata=metadata,
        )

    async def content(
        self,
        hits: list[SearchHit],
        *,
        query: str | None = None,
    ) -> list[ContentSnippet]:
        module = self._module()
        urls = [hit.url for hit in hits if hit.url]
        if not urls:
            return []
        raw = await asyncio.to_thread(
            module.content.snippets,
            query or "relevant information",
            urls,
            max_tokens=max(1000, 1000 * len(urls)),
            max_tokens_per_page=1000,
        )
        by_url = {hit.url: hit for hit in hits}
        snippets = []
        for item in raw:
            url = str(getattr(item, "url", "") or "")
            source = by_url.get(url)
            snippets.append(
                ContentSnippet(
                    ref=source.ref if source else "",
                    text=str(getattr(item, "text", "") or getattr(item, "snippet", "")),
                    url=url or None,
                    title=source.title if source else "",
                    metadata=item.model_dump() if hasattr(item, "model_dump") else {},
                )
            )
        return snippets
