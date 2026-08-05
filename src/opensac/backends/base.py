from __future__ import annotations

from typing import Protocol

from opensac_sdk.models import ContentSnippet, SearchHit


class SearchBackend(Protocol):
    name: str

    async def search(
        self,
        query: str,
        *,
        limit: int,
        domains: list[str] | None = None,
    ) -> list[SearchHit]: ...

    async def content(
        self,
        hits: list[SearchHit],
        *,
        query: str | None = None,
    ) -> list[ContentSnippet]: ...
