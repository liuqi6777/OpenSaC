from __future__ import annotations

from typing import Any

from opensac.backends.search import SearchHit
from opensac.broker.call_context import current_call
from opensac.broker.failures import CapabilityFailure
from opensac.broker.services import SearchService
from opensac.broker.session import BrokerSession, FlightGroup
from opensac.broker.sources import document_handle_for_hit, document_identity, source_for
from opensac.broker.validation import integer, optional_string_list, string
from opensac.provider import ProviderRequestError
from opensac.tracing import HitRecord, ProviderAttemptRecord

from ..providers.execution import CapabilityProviderError

type _SearchOutcome = list[SearchHit] | CapabilityFailure


def _provider_attempts() -> list[ProviderAttemptRecord]:
    context = current_call()
    return context.provider_attempts if context is not None else []


class SearchCapabilities:
    """Implement search capabilities against the session's admitted backend."""

    def __init__(
        self,
        services: dict[str, SearchService],
        *,
        max_query_chars: int,
        max_top_k: int,
    ) -> None:
        if not services:
            raise ValueError("at least one search service must be configured")
        self.services = services
        self.max_search_query_chars = max_query_chars
        self.max_search_top_k = max_top_k
        self.inflight_coalescing = next(iter(services.values())).inflight_coalescing

    def _search_service(self, state: BrokerSession) -> tuple[str, SearchService]:
        """The one service this session searches.

        Resolved from the session rather than from the method name, which is
        what makes `search.query` backend-neutral. The deployment records its
        one configured backend on every session, so there is nothing to choose
        between here; a session that somehow holds two is a bug worth stopping
        on rather than a tie to break arbitrarily.
        """
        names = sorted(state.policy.allowed_backends & set(self.services))
        if len(names) != 1:
            raise RuntimeError(
                "A session must have exactly one configured search backend, "
                f"this one has {names or sorted(state.policy.allowed_backends)}."
            )
        return names[0], self.services[names[0]]

    async def query(self, state: BrokerSession, params: dict[str, Any]) -> list[dict[str, Any]]:
        return await self._search(state, params)

    def _prepare_search(
        self,
        state: BrokerSession,
        params: dict[str, Any],
    ) -> tuple[str, SearchService, str, list[str] | None, int, int]:
        """Validate one query without executing it or charging usage."""
        backend_name, service = self._search_service(state)
        state.policy.require_backend(backend_name)
        query = string(
            params.get("query", ""),
            "query",
            strip=True,
            max_chars=self.max_search_query_chars,
        )
        domains = optional_string_list(
            params.get("include_domains"),
            "include_domains",
        )
        # Refused rather than dropped. A backend-neutral method name is only
        # honest if a parameter it cannot honour fails loudly: a program that
        # asked for one site and silently got the whole web draws exactly the
        # wrong conclusion from an empty result.
        if domains and not service.supports_domains:
            raise ValueError(
                f"The '{backend_name}' backend has no domain filter, so "
                f"include_domains={list(domains)!r} cannot be honoured. Drop the argument "
                "and filter the hits in Python, or put the constraint in the query."
            )
        limit, offset = self._search_window(params)
        # Same rule as `domains`, for the other thing a backend cannot honour.
        # Clipping would let a program believe it read rank 150 and conclude the
        # document is absent, when nothing ever looked past the ceiling.
        depth = offset + limit
        if service.max_depth is not None and depth > service.max_depth:
            raise ValueError(
                f"The '{backend_name}' backend reaches rank {service.max_depth} at "
                f"most, and offset={offset} with limit={limit} asks for {depth}. "
                "Narrow the window, or find the document with a different query."
            )
        return backend_name, service, query, domains, limit, offset

    def _search_window(
        self,
        params: dict[str, Any],
        *,
        limit_key: str = "limit",
    ) -> tuple[int, int]:
        """Validate a requested retrieval window before clipping or fan-out."""
        limit = integer(params.get(limit_key, 10), limit_key, minimum=1, maximum=100)
        offset = integer(params.get("offset", 0), "offset", minimum=0, maximum=500)
        depth = offset + limit
        if depth > self.max_search_top_k:
            raise ValueError(
                f"offset={offset} with {limit_key}={limit} asks for retrieval "
                f"depth {depth}, exceeding the broker maximum of "
                f"{self.max_search_top_k}"
            )
        return limit, offset

    def _record_search_hits(
        self,
        state: BrokerSession,
        hits: list[SearchHit],
        *,
        query_index: int | None = None,
    ) -> list[dict[str, Any]]:
        context = current_call()
        recorded = context.hits if context is not None else None
        wire_hits: list[dict[str, Any]] = []
        for hit in hits:
            candidate_source = source_for(hit)
            handle = document_handle_for_hit(hit, source=candidate_source)
            identity = document_identity(hit.backend, handle)
            source = state.remember(
                hit.backend,
                handle,
                identity=identity,
                rank=hit.rank,
                score=hit.score,
            )
            admitted_hit = hit.model_copy(update={"source": source})
            if recorded is not None:
                recorded.append(
                    HitRecord(
                        identity=identity,
                        rank=hit.rank,
                        score=hit.score,
                        query_index=query_index,
                        retrieval_mode=self._effective_retrieval_mode(hit),
                        admission="search",
                    )
                )
            wire_hits.append(self._search_hit_wire(admitted_hit))
        return wire_hits

    @staticmethod
    def _search_hit_wire(hit: SearchHit) -> dict[str, Any]:
        """Expose one source address without leaking alternate identity fields."""

        payload = hit.model_dump(mode="json", exclude={"url", "docid"})
        metadata = dict(payload.get("metadata") or {})
        for key in ("ref", "url", "docid", "source"):
            metadata.pop(key, None)
        payload["metadata"] = metadata
        return payload

    @staticmethod
    def _effective_retrieval_mode(hit: SearchHit) -> str | None:
        retrieval = getattr(hit, "retrieval", None)
        if retrieval is None:
            return None
        if isinstance(retrieval, dict):
            return retrieval.get("mode") or retrieval.get("result_mode")
        return getattr(retrieval, "mode", None) or getattr(retrieval, "result_mode", None)

    async def _retrieve_search(
        self,
        state: BrokerSession,
        params: dict[str, Any],
        *,
        request_index: int = 0,
        record_usage: bool = True,
        request_id: str | None = None,
        track_execution: bool = True,
    ) -> list[SearchHit]:
        """Execute one search without mutating the session reference table."""
        backend_name, service = self._search_service(state)
        state.policy.require_backend(backend_name)
        _, prepared_service, query, domains, limit, offset = self._prepare_search(state, params)
        if prepared_service is not service:
            raise RuntimeError("Session search service changed during request preparation.")
        if record_usage:
            await state.policy.record_search()

        return await service.search(
            state,
            query,
            limit=limit,
            offset=offset,
            domains=domains,
            request_index=request_index,
            request_id=request_id,
            track_execution=track_execution,
        )

    async def _search(
        self,
        state: BrokerSession,
        params: dict[str, Any],
    ) -> list[dict[str, Any]]:
        if self.inflight_coalescing:
            return await self._search_coalesced(state, params)
        hits = await self._retrieve_search(state, params)
        return self._record_search_hits(state, hits)

    async def _search_coalesced(
        self,
        state: BrokerSession,
        params: dict[str, Any],
    ) -> list[dict[str, Any]]:
        backend_name, service, query, domains, limit, offset = self._prepare_search(state, params)
        await state.policy.record_search()
        normalized_domains = sorted(set(domains)) if domains else None
        fingerprint = service.request_fingerprint(
            query,
            limit=limit,
            offset=offset,
            domains=normalized_domains,
        )
        key = service.flight_key(fingerprint)
        admission = await service.flights.admit(
            state,
            {key: (fingerprint, [0])},
            group_new=False,
        )
        for group in admission.new_groups:

            async def execute(
                group: FlightGroup = group,
            ) -> dict[str, _SearchOutcome]:
                try:
                    hits = await self._retrieve_search(
                        state,
                        {
                            "query": query,
                            "limit": limit,
                            "offset": offset,
                            "include_domains": normalized_domains,
                        },
                        record_usage=False,
                        request_id=group.request_id,
                        track_execution=False,
                    )
                except ProviderRequestError as exc:
                    outcome: _SearchOutcome = CapabilityFailure.model_validate(
                        service.provider_failure(exc)
                    )
                else:
                    outcome = hits
                return {key: outcome}

            service.flights.start(state, group, execute)

        outcome = await service.flights.wait(state, admission.waiters[key])
        if isinstance(outcome, CapabilityFailure):
            raise CapabilityProviderError.from_failure(
                outcome.model_dump(mode="json"),
                attempts=len(_provider_attempts()),
            )
        if not isinstance(outcome, list) or not all(isinstance(hit, SearchHit) for hit in outcome):
            raise RuntimeError("Search flight returned an invalid outcome.")
        return self._record_search_hits(state, outcome)
