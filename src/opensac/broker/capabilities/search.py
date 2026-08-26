from __future__ import annotations

import asyncio
import copy
import uuid
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from opensac.backends.search import RetrievalMetadata, SearchBatch, SearchBatchFailure, SearchHit
from opensac.broker.call_context import current_call
from opensac.broker.failures import CapabilityFailure
from opensac.broker.services import SearchService
from opensac.broker.session import BrokerSession, FlightGroup
from opensac.broker.sources import document_handle_for_hit, document_identity, source_for
from opensac.broker.validation import integer, optional_string_list, string
from opensac.provider import ProviderRequestError
from opensac.tracing import HitRecord, ProviderAttemptRecord

from ..providers.execution import CapabilityProviderError


class SearchResultHit(BaseModel):
    """Public search hit without provider locator fields."""

    model_config = ConfigDict(extra="forbid")

    source: str
    backend: str
    title: str = ""
    domain: str | None = None
    date: str | None = None
    snippet: str = ""
    score: float | None = None
    rank: int
    retrieval: RetrievalMetadata | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class SearchResult(BaseModel):
    """One successful query in a multi-query search report."""

    model_config = ConfigDict(extra="forbid")

    input_index: int = Field(ge=0)
    query: str
    hits: list[SearchResultHit] = Field(default_factory=list)


class SearchFailure(CapabilityFailure):
    """One failed query in a multi-query search report."""

    input_index: int = Field(ge=0)
    query: str


class SearchReport(BaseModel):
    """Successful and failed query outcomes, partitioned by input index."""

    model_config = ConfigDict(extra="forbid")

    results: list[SearchResult] = Field(default_factory=list)
    failures: list[SearchFailure] = Field(default_factory=list)
    input_count: int = Field(ge=0)

    @model_validator(mode="after")
    def _validate_partition(self) -> Self:
        indexes = [row.input_index for row in self.results]
        indexes.extend(row.input_index for row in self.failures)
        if sorted(indexes) != list(range(self.input_count)):
            raise ValueError("search results and failures must partition the input indexes")
        return self


type _SearchOutcome = SearchBatch | CapabilityFailure


def _provider_attempts() -> list[ProviderAttemptRecord]:
    context = current_call()
    return context.provider_attempts if context is not None else []


class SearchCapabilities:
    """Implement search capabilities against the session's admitted backend."""

    def __init__(
        self,
        services: dict[str, SearchService],
        *,
        max_queries_per_request: int,
        max_query_chars: int,
        max_top_k: int,
    ) -> None:
        if not services:
            raise ValueError("at least one search service must be configured")
        self.services = services
        self.max_search_queries_per_request = max_queries_per_request
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
        domains = optional_string_list(params.get("domains"), "domains")
        # Refused rather than dropped. A backend-neutral method name is only
        # honest if a parameter it cannot honour fails loudly: a program that
        # asked for one site and silently got the whole web draws exactly the
        # wrong conclusion from an empty result.
        if domains and not service.supports_domains:
            raise ValueError(
                f"The '{backend_name}' backend has no domain filter, so "
                f"domains={list(domains)!r} cannot be honoured. Drop the argument "
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
                            "domains": normalized_domains,
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
                    outcome = SearchBatch(query=query, hits=hits)
                return {key: outcome}

            service.flights.start(state, group, execute)

        outcome = await service.flights.wait(state, admission.waiters[key])
        if isinstance(outcome, CapabilityFailure):
            raise CapabilityProviderError.from_failure(
                outcome.model_dump(mode="json"),
                attempts=len(_provider_attempts()),
            )
        if not isinstance(outcome, SearchBatch):
            raise RuntimeError("Search flight returned an invalid outcome.")
        return self._record_search_hits(state, list(outcome.hits))

    async def query_many(self, state: BrokerSession, params: dict[str, Any]) -> dict[str, Any]:
        return await self._search_many(state, params)

    async def _search_many(
        self,
        state: BrokerSession,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        raw_queries = params.get("queries", [])
        if not isinstance(raw_queries, list):
            raise ValueError("queries must be a list")
        if len(raw_queries) > self.max_search_queries_per_request:
            raise ValueError(
                f"query_many contains {len(raw_queries)} queries, exceeding the "
                f"broker maximum of {self.max_search_queries_per_request}"
            )
        if any(not isinstance(query, str) for query in raw_queries):
            raise ValueError("queries must contain only strings")
        queries = list(raw_queries)
        limit, offset = self._search_window(params, limit_key="limit_per_query")
        domains_value = optional_string_list(params.get("domains"), "domains")
        domains = sorted(set(domains_value)) if domains_value else None
        concurrency = integer(
            params.get("concurrency", 5),
            "concurrency",
            minimum=1,
            maximum=20,
        )
        backend_name, service = self._search_service(state)
        state.policy.require_backend(backend_name)
        # Options are global to the batch. Validate them once before charging
        # usage or admitting any provider side effect.
        self._prepare_search(
            state,
            {
                "query": "x",
                "limit": limit,
                "offset": offset,
                "domains": domains,
            },
        )
        await state.policy.record_search(len(queries))

        outcomes: list[_SearchOutcome | None] = [None] * len(queries)
        leaders: list[int] = []
        leader_for_key: dict[str, int] = {}
        fingerprints: dict[int, str] = {}
        for index, original_query in enumerate(queries):
            query = original_query.strip()
            if not query:
                outcomes[index] = CapabilityFailure.model_validate(
                    service.contextualize_failure(
                        {
                            "code": "invalid_request",
                            "message": "query must not be empty",
                            "retryable": False,
                            "attempts": 0,
                        },
                    )
                )
                continue
            if len(query) > self.max_search_query_chars:
                outcomes[index] = CapabilityFailure.model_validate(
                    service.contextualize_failure(
                        {
                            "code": "invalid_request",
                            "message": (
                                f"query has {len(query)} characters, exceeding the broker "
                                f"maximum of {self.max_search_query_chars}"
                            ),
                            "retryable": False,
                            "attempts": 0,
                        },
                    )
                )
                continue
            fingerprint = service.request_fingerprint(
                query,
                limit=limit,
                offset=offset,
                domains=domains,
            )
            fingerprints[index] = fingerprint
            leader = leader_for_key.get(fingerprint)
            if leader is None:
                leader_for_key[fingerprint] = index
                leaders.append(index)
                continue
            service.record_deduplicated_request(
                request_index=index,
                leader_index=leader,
                request_fingerprint=fingerprint,
            )

        duplicate_count = len(fingerprints) - len(leaders)
        if duplicate_count:
            state.policy.record_deduplicated(duplicate_count)

        if self.inflight_coalescing:
            leader_rows = await self._search_many_coalesced(
                state,
                service,
                queries,
                leaders,
                fingerprints,
                limit=limit,
                offset=offset,
                domains=domains,
                concurrency=concurrency,
            )
        elif leaders and service.supports_batch:
            leader_rows = await self._search_many_transport_batch(
                state,
                service,
                queries,
                leaders,
                limit=limit,
                offset=offset,
                domains=domains,
            )
        else:
            gate = asyncio.Semaphore(concurrency)

            async def one(index: int) -> _SearchOutcome:
                async with gate:
                    try:
                        hits = await self._retrieve_search(
                            state,
                            {
                                "query": queries[index],
                                "limit": limit,
                                "offset": offset,
                                "domains": domains,
                            },
                            request_index=index,
                            record_usage=False,
                        )
                    except ProviderRequestError as exc:
                        return CapabilityFailure.model_validate(service.provider_failure(exc))
                    return SearchBatch(
                        query=queries[index],
                        hits=hits,
                    )

            returned = await asyncio.gather(*(one(index) for index in leaders))
            leader_rows = dict(zip(leaders, returned, strict=True))

        for index in leaders:
            outcomes[index] = leader_rows[index]
        for index, fingerprint in fingerprints.items():
            if index in leader_rows:
                continue
            leader = leader_for_key[fingerprint]
            outcomes[index] = copy.deepcopy(leader_rows[leader])

        assert all(outcome is not None for outcome in outcomes)
        results: list[SearchResult] = []
        failures: list[SearchFailure] = []
        for input_index, outcome in enumerate(outcomes):
            assert outcome is not None
            if isinstance(outcome, CapabilityFailure):
                failures.append(
                    SearchFailure(
                        input_index=input_index,
                        query=queries[input_index],
                        **outcome.model_dump(mode="json"),
                    )
                )
                continue
            hits = self._record_search_hits(
                state,
                list(outcome.hits),
                query_index=input_index,
            )
            results.append(
                SearchResult(
                    input_index=input_index,
                    query=queries[input_index],
                    hits=hits,
                )
            )
        return SearchReport(
            results=results,
            failures=failures,
            input_count=len(queries),
        ).model_dump(mode="json")

    async def _search_many_coalesced(
        self,
        state: BrokerSession,
        service: SearchService,
        queries: list[str],
        leaders: list[int],
        fingerprints: dict[int, str],
        *,
        limit: int,
        offset: int,
        domains: list[str] | None,
        concurrency: int,
    ) -> dict[int, _SearchOutcome]:
        """Attach active keys and send the remaining batch-capable keys together."""

        key_for_index = {index: service.flight_key(fingerprints[index]) for index in leaders}
        admission = await service.flights.admit(
            state,
            {key_for_index[index]: (fingerprints[index], [index]) for index in leaders},
            group_new=service.supports_batch,
        )
        index_for_key = {key: index for index, key in key_for_index.items()}
        gate = asyncio.Semaphore(concurrency)
        for group in admission.new_groups:
            group_indexes = [index_for_key[key] for key in group.keys]
            group_indexes.sort()
            if service.supports_batch:

                async def execute_batch(
                    group: FlightGroup = group,
                    group_indexes: list[int] = group_indexes,
                ) -> dict[str, _SearchOutcome]:
                    rows = await self._search_many_transport_batch(
                        state,
                        service,
                        queries,
                        group_indexes,
                        limit=limit,
                        offset=offset,
                        domains=domains,
                        request_id=group.request_id,
                        track_execution=False,
                    )
                    return {key_for_index[index]: row for index, row in rows.items()}

                service.flights.start(state, group, execute_batch)
                continue

            if len(group_indexes) != 1:
                raise RuntimeError("non-batch search flight contains multiple keys")
            index = group_indexes[0]
            key = key_for_index[index]

            async def execute_one(
                group: FlightGroup = group,
                index: int = index,
                key: str = key,
            ) -> dict[str, _SearchOutcome]:
                async with gate:
                    try:
                        hits = await self._retrieve_search(
                            state,
                            {
                                "query": queries[index],
                                "limit": limit,
                                "offset": offset,
                                "domains": domains,
                            },
                            request_index=index,
                            record_usage=False,
                            request_id=group.request_id,
                            track_execution=False,
                        )
                    except ProviderRequestError as exc:
                        outcome: _SearchOutcome = CapabilityFailure.model_validate(
                            service.provider_failure(exc)
                        )
                    else:
                        outcome = SearchBatch(
                            query=queries[index],
                            hits=hits,
                        )
                return {key: outcome}

            service.flights.start(state, group, execute_one)

        returned = await asyncio.gather(
            *(
                service.flights.wait(state, admission.waiters[key_for_index[index]])
                for index in leaders
            )
        )
        rows: dict[int, _SearchOutcome] = {}
        for index, result in zip(leaders, returned, strict=True):
            if not isinstance(result, (SearchBatch, CapabilityFailure)):
                raise RuntimeError("Search flight returned an invalid outcome.")
            rows[index] = result
        return rows

    async def _search_many_transport_batch(
        self,
        state: BrokerSession,
        service: SearchService,
        queries: list[str],
        leaders: list[int],
        *,
        limit: int,
        offset: int,
        domains: list[str] | None,
        request_id: str | None = None,
        track_execution: bool = True,
    ) -> dict[int, _SearchOutcome]:
        """Execute unique queries as one real provider transport batch."""

        _backend_name, resolved = self._search_service(state)
        if resolved is not service:
            raise RuntimeError("Session search service changed during batch execution.")
        unique_queries = [queries[index].strip() for index in leaders]
        request_id = request_id or f"req_{uuid.uuid4().hex}"

        try:
            returned = await service.search_many(
                state,
                unique_queries,
                request_indexes=leaders,
                limit=limit,
                offset=offset,
                domains=domains,
                request_id=request_id,
                track_execution=track_execution,
            )
        except ProviderRequestError as exc:
            failure = CapabilityFailure.model_validate(service.provider_failure(exc))
            return {index: failure for index in leaders}
        if len(returned) != len(leaders):
            raise RuntimeError("Provider runtime accepted an invalid batch result count.")
        attempts = max(
            (record.attempt for record in _provider_attempts() if record.request_id == request_id),
            default=1,
        )
        rows: dict[int, _SearchOutcome] = {}
        for index, outcome in zip(leaders, returned, strict=True):
            if isinstance(outcome, SearchBatchFailure):
                failure = CapabilityFailure.model_validate(
                    service.contextualize_failure(
                        outcome.model_dump(mode="json") | {"attempts": attempts},
                    )
                )
                if failure.code == "provider_rejected":
                    failure = failure.model_copy(
                        update={"message": "Provider rejected one search item."}
                    )
                rows[index] = failure
            elif isinstance(outcome, SearchBatch):
                rows[index] = outcome
            else:
                raise RuntimeError("Search backend returned an invalid batch outcome.")
        if any(isinstance(row, CapabilityFailure) for row in rows.values()):
            for record in reversed(_provider_attempts()):
                if record.request_id == request_id and record.status == "success":
                    record.status = "partial"
                    break
        return rows
