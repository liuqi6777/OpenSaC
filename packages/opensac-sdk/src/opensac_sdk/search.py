from __future__ import annotations

from .models import SearchBatch, SearchHit
from .transport import UnixSocketTransport


class SearchResource:
    def __init__(self, transport: UnixSocketTransport) -> None:
        self._transport = transport

    def web(
        self,
        query: str,
        *,
        limit: int = 10,
        domains: list[str] | None = None,
    ) -> list[SearchHit]:
        result = self._transport.call(
            "search.web",
            {"query": query, "limit": limit, "domains": domains},
        )
        return [SearchHit.model_validate(hit) for hit in result]

    def local(self, query: str, *, limit: int = 10) -> list[SearchHit]:
        result = self._transport.call("search.local", {"query": query, "limit": limit})
        return [SearchHit.model_validate(hit) for hit in result]

    def web_many(
        self,
        queries: list[str],
        *,
        limit_per_query: int = 10,
        concurrency: int = 5,
    ) -> list[SearchBatch]:
        result = self._transport.call(
            "search.web_many",
            {
                "queries": queries,
                "limit_per_query": limit_per_query,
                "concurrency": concurrency,
            },
        )
        return [SearchBatch.model_validate(batch) for batch in result]

    def local_many(
        self,
        queries: list[str],
        *,
        limit_per_query: int = 10,
        concurrency: int = 5,
    ) -> list[SearchBatch]:
        result = self._transport.call(
            "search.local_many",
            {
                "queries": queries,
                "limit_per_query": limit_per_query,
                "concurrency": concurrency,
            },
        )
        return [SearchBatch.model_validate(batch) for batch in result]
