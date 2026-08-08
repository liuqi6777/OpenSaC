from __future__ import annotations

from .models import SearchBatch, SearchHit
from .transport import UnixSocketTransport


class SearchResource:
    """Retrieval, the only way a document enters this session's reach.

    `offset` is depth into the ranking, and it matters more than paging usually
    does: a document becomes fetchable only by being returned from a search, so
    `limit` is at once how much you can see and how much you are allowed to
    read. Without `offset`, a program certain the answer sits at rank 15 has no
    way to get there. `rank` is always the rank in the full result list, never
    the position within the returned window.
    """

    def __init__(self, transport: UnixSocketTransport) -> None:
        self._transport = transport

    def web(
        self,
        query: str,
        *,
        limit: int = 10,
        offset: int = 0,
        domains: list[str] | None = None,
    ) -> list[SearchHit]:
        result = self._transport.call(
            "search.web",
            {"query": query, "limit": limit, "offset": offset, "domains": domains},
        )
        return [SearchHit.model_validate(hit) for hit in result]

    def local(self, query: str, *, limit: int = 10, offset: int = 0) -> list[SearchHit]:
        result = self._transport.call(
            "search.local",
            {"query": query, "limit": limit, "offset": offset},
        )
        return [SearchHit.model_validate(hit) for hit in result]

    def web_many(
        self,
        queries: list[str],
        *,
        limit_per_query: int = 10,
        offset: int = 0,
        concurrency: int = 5,
        domains: list[str] | None = None,
    ) -> list[SearchBatch]:
        result = self._transport.call(
            "search.web_many",
            {
                "queries": queries,
                "limit_per_query": limit_per_query,
                "offset": offset,
                "concurrency": concurrency,
                "domains": domains,
            },
        )
        return [SearchBatch.model_validate(batch) for batch in result]

    def local_many(
        self,
        queries: list[str],
        *,
        limit_per_query: int = 10,
        offset: int = 0,
        concurrency: int = 5,
    ) -> list[SearchBatch]:
        result = self._transport.call(
            "search.local_many",
            {
                "queries": queries,
                "limit_per_query": limit_per_query,
                "offset": offset,
                "concurrency": concurrency,
            },
        )
        return [SearchBatch.model_validate(batch) for batch in result]
