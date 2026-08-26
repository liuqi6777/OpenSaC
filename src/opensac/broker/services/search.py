from __future__ import annotations

from pydantic import TypeAdapter

from opensac.backends.search.base import (
    BatchSearchBackend,
    SearchBackend,
    SearchBatchOutcome,
    SearchHit,
)
from opensac.broker.providers.execution import ProviderExecutor
from opensac.broker.session import BrokerSession
from opensac.provider import ProviderRuntime

from .base import ServiceExecution

_SEARCH_HITS = TypeAdapter(list[SearchHit])
_SEARCH_BATCH_OUTCOMES = TypeAdapter(list[SearchBatchOutcome])


class SearchService(ServiceExecution):
    """Reusable search service with backend-neutral execution policy."""

    component = "search"

    def __init__(
        self,
        route: str,
        backend: SearchBackend,
        providers: ProviderExecutor,
        runtime: ProviderRuntime,
        *,
        backend_revision: str,
    ) -> None:
        super().__init__(backend, providers, runtime)
        self.route = route
        self.backend_revision = backend_revision

    def request_fingerprint(
        self,
        query: str,
        *,
        limit: int,
        offset: int,
        domains: list[str] | None,
    ) -> str:
        return self.fingerprint(
            self._request_value(
                query,
                limit=limit,
                offset=offset,
                domains=domains,
            )
        )

    def _request_value(
        self,
        query: str,
        *,
        limit: int,
        offset: int,
        domains: list[str] | None,
    ) -> dict[str, object]:
        return {
            "backend": self.route,
            "revision": self.backend_revision,
            "query": query,
            "limit": limit,
            "offset": offset,
            "domains": domains,
        }

    @property
    def supports_domains(self) -> bool:
        return self.backend.supports_domains

    @property
    def max_depth(self) -> int | None:
        return self.backend.max_depth

    @property
    def supports_batch(self) -> bool:
        return isinstance(self.backend, BatchSearchBackend)

    async def search(
        self,
        state: BrokerSession,
        query: str,
        *,
        limit: int,
        offset: int,
        domains: list[str] | None,
        request_index: int,
        request_id: str | None = None,
        track_execution: bool = True,
    ) -> list[SearchHit]:
        async def request() -> list[SearchHit]:
            return _SEARCH_HITS.validate_python(
                await self.backend.search(
                    query,
                    limit=limit,
                    offset=offset,
                    domains=domains,
                ),
                strict=True,
            )

        preflight = getattr(self.backend, "preflight_search", None)
        return await self.run(
            state,
            request_indexes=[request_index],
            request_value=self._request_value(
                query,
                limit=limit,
                offset=offset,
                domains=domains,
            ),
            request=request,
            preflight=preflight if callable(preflight) else None,
            request_id=request_id,
            track_execution=track_execution,
        )

    async def search_many(
        self,
        state: BrokerSession,
        queries: list[str],
        *,
        request_indexes: list[int],
        limit: int,
        offset: int,
        domains: list[str] | None,
        request_id: str | None = None,
        track_execution: bool = True,
    ) -> list[SearchBatchOutcome]:
        backend = self.backend
        if not isinstance(backend, BatchSearchBackend):
            raise TypeError("search backend does not support transport batching")

        async def request() -> list[SearchBatchOutcome]:
            return _SEARCH_BATCH_OUTCOMES.validate_python(
                await backend.search_many(
                    queries,
                    limit=limit,
                    offset=offset,
                    domains=domains,
                ),
                strict=True,
            )

        preflight = getattr(backend, "preflight_search", None)
        return await self.run(
            state,
            request_indexes=request_indexes,
            request_value={
                "backend": self.route,
                "revision": self.backend_revision,
                "queries": queries,
                "limit": limit,
                "offset": offset,
                "domains": domains,
            },
            request=request,
            preflight=preflight if callable(preflight) else None,
            request_id=request_id,
            track_execution=track_execution,
        )
