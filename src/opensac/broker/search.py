from __future__ import annotations

import asyncio
import copy
import uuid
from typing import Any

from opensac._contracts import SearchBatch, SearchHit
from opensac.backends.search.base import BatchSearchBackend, SearchBackend
from opensac.broker.call_context import current_call
from opensac.broker.documents import document_identity, source_for
from opensac.broker.provider_execution import CapabilityProviderError, ProviderExecutor
from opensac.broker.session import BrokerSession, FlightGroup
from opensac.models import HitRecord, ProviderAttemptRecord
from opensac.provider import ProviderRequestError


def _provider_attempts() -> list[ProviderAttemptRecord]:
    context = current_call()
    return context.provider_attempts if context is not None else []


class SearchCapabilities:
    """Implement search capabilities against the session's admitted backend."""

    def __init__(
        self,
        backends: dict[str, SearchBackend],
        providers: ProviderExecutor,
        *,
        backend_revision: str,
        max_queries_per_request: int,
        max_query_chars: int,
        max_top_k: int,
    ) -> None:
        self.backends = backends
        self.providers = providers
        self.backend_revision = backend_revision
        self.max_search_queries_per_request = max_queries_per_request
        self.max_search_query_chars = max_query_chars
        self.max_search_top_k = max_top_k
        self.inflight_coalescing = providers.inflight_coalescing

    def _search_backend(self, state: BrokerSession) -> tuple[str, SearchBackend]:
        """The one backend this session searches.

        Resolved from the session rather than from the method name, which is
        what makes `search.query` backend-neutral. The deployment records its
        one configured backend on every session, so there is nothing to choose
        between here; a session that somehow holds two is a bug worth stopping
        on rather than a tie to break arbitrarily.
        """
        names = sorted(state.policy.allowed_backends & set(self.backends))
        if len(names) != 1:
            raise RuntimeError(
                "A session must have exactly one configured search backend, "
                f"this one has {names or sorted(state.policy.allowed_backends)}."
            )
        return names[0], self.backends[names[0]]

    async def query(self, state: BrokerSession, params: dict[str, Any]) -> list[dict[str, Any]]:
        return await self._search(state, params)

    def _prepare_search(
        self,
        state: BrokerSession,
        params: dict[str, Any],
    ) -> tuple[str, SearchBackend, str, list[str] | None, int, int]:
        """Validate one query without executing it or charging usage."""
        backend_name, backend = self._search_backend(state)
        state.policy.require_backend(backend_name)
        query = str(params.get("query", "")).strip()
        if not query:
            raise ValueError("query must not be empty")
        if len(query) > self.max_search_query_chars:
            raise ValueError(
                f"query has {len(query)} characters, exceeding the broker maximum "
                f"of {self.max_search_query_chars}"
            )
        domains = params.get("domains")
        if domains is not None and not isinstance(domains, list):
            raise ValueError("domains must be a list when provided")
        # Refused rather than dropped. A backend-neutral method name is only
        # honest if a parameter it cannot honour fails loudly: a program that
        # asked for one site and silently got the whole web draws exactly the
        # wrong conclusion from an empty result.
        if domains and not backend.supports_domains:
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
        if backend.max_depth is not None and depth > backend.max_depth:
            raise ValueError(
                f"The '{backend_name}' backend reaches rank {backend.max_depth} at "
                f"most, and offset={offset} with limit={limit} asks for {depth}. "
                "Narrow the window, or find the document with a different query."
            )
        return backend_name, backend, query, domains, limit, offset

    def _search_window(
        self,
        params: dict[str, Any],
        *,
        limit_key: str = "limit",
    ) -> tuple[int, int]:
        """Validate a requested retrieval window before clipping or fan-out."""
        limit = max(int(params.get(limit_key, 10)), 1)
        offset = max(int(params.get("offset", 0)), 0)
        if limit > 100:
            raise ValueError(f"{limit_key} must be at most 100, got {limit}")
        if offset > 500:
            raise ValueError(f"offset must be at most 500, got {offset}")
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
        for hit in hits:
            candidate_source = source_for(hit)
            identity = document_identity(hit)
            hit.source = state.remember(
                hit,
                identity=identity,
                candidate_source=candidate_source,
            )
            if recorded is not None:
                recorded.append(
                    HitRecord(
                        identity=identity,
                        rank=hit.rank,
                        score=hit.score,
                        query_index=query_index,
                        retrieval_mode=self._effective_retrieval_mode(hit),
                    )
                )
        return [self._search_hit_wire(hit) for hit in hits]

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
        operation_id: str | None = None,
        track_execution: bool = True,
    ) -> list[SearchHit]:
        """Execute one search without mutating the session reference table."""
        backend_name, backend = self._search_backend(state)
        state.policy.require_backend(backend_name)
        _, prepared_backend, query, domains, limit, offset = self._prepare_search(state, params)
        if prepared_backend is not backend:
            raise RuntimeError("Session search backend changed during request preparation.")
        if record_usage:
            await state.policy.record_search()

        async def request() -> list[SearchHit]:
            return await backend.search(
                query,
                limit=limit,
                offset=offset,
                domains=domains,
            )

        preflight = getattr(backend, "preflight_search", None)

        return await self.providers.run(
            state,
            backend=backend,
            operation=f"{backend_name}.search",
            request_indexes=[request_index],
            request_value={
                "backend": backend_name,
                "revision": self.backend_revision,
                "query": query,
                "limit": limit,
                "offset": offset,
                "domains": domains,
            },
            request=request,
            preflight=preflight if callable(preflight) else None,
            operation_id=operation_id,
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
        backend_name, _backend, query, domains, limit, offset = self._prepare_search(state, params)
        await state.policy.record_search()
        normalized_domains = (
            sorted({str(domain).strip() for domain in domains if str(domain).strip()})
            if domains
            else None
        )
        request = {"limit": limit, "offset": offset, "domains": normalized_domains}
        fingerprint = self.providers.fingerprint(
            {
                "backend": backend_name,
                "revision": self.backend_revision,
                "query": query,
                **request,
            }
        )
        key = self.providers.flight_key(f"{backend_name}.search", fingerprint)
        admission = await self.providers.admit_flights(
            state,
            {key: (fingerprint, [0])},
            group_new=False,
        )
        for group in admission.new_groups:

            async def execute(
                group: FlightGroup = group,
            ) -> dict[str, SearchBatch]:
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
                        operation_id=group.operation_id,
                        track_execution=False,
                    )
                except ProviderRequestError as exc:
                    batch = SearchBatch(
                        query=query,
                        failure=self.providers.provider_failure(exc),
                    )
                else:
                    batch = SearchBatch(query=query, hits=hits)
                return {key: batch}

            self.providers.start_flight_group(state, group, execute)

        batch = SearchBatch.model_validate(
            await self.providers.await_flight(state, admission.waiters[key])
        )
        if batch.failure is not None:
            raise CapabilityProviderError.from_failure(
                batch.failure.model_dump(mode="json"),
                attempts=len(_provider_attempts()),
            )
        return self._record_search_hits(state, list(batch.hits))

    async def query_many(
        self, state: BrokerSession, params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        return await self._search_many(state, params)

    async def _search_many(
        self,
        state: BrokerSession,
        params: dict[str, Any],
    ) -> list[dict[str, Any]]:
        raw_queries = params.get("queries", [])
        if not isinstance(raw_queries, list):
            raise ValueError("queries must be a list")
        if len(raw_queries) > self.max_search_queries_per_request:
            raise ValueError(
                f"query_many contains {len(raw_queries)} queries, exceeding the "
                f"broker maximum of {self.max_search_queries_per_request}"
            )
        queries = [str(query) for query in raw_queries]
        limit, offset = self._search_window(params, limit_key="limit_per_query")
        domains_value = params.get("domains")
        if domains_value is not None and not isinstance(domains_value, list):
            raise ValueError("domains must be a list when provided")
        domains = (
            sorted({str(domain).strip() for domain in domains_value if str(domain).strip()})
            if domains_value
            else None
        )
        backend_name, backend = self._search_backend(state)
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

        request = {
            "limit": limit,
            "offset": offset,
            "domains": domains,
        }
        batches: list[SearchBatch | None] = [None] * len(queries)
        leaders: list[int] = []
        leader_for_key: dict[str, int] = {}
        fingerprints: dict[int, str] = {}
        for index, original_query in enumerate(queries):
            query = original_query.strip()
            if not query:
                batches[index] = SearchBatch(
                    query=original_query,
                    failure={
                        "code": "invalid_request",
                        "message": "query must not be empty",
                        "retryable": False,
                        "attempts": 0,
                    },
                )
                continue
            if len(query) > self.max_search_query_chars:
                batches[index] = SearchBatch(
                    query=original_query,
                    failure={
                        "code": "invalid_request",
                        "message": (
                            f"query has {len(query)} characters, exceeding the broker "
                            f"maximum of {self.max_search_query_chars}"
                        ),
                        "retryable": False,
                        "attempts": 0,
                    },
                )
                continue
            fingerprint = self.providers.fingerprint(
                {
                    "backend": backend_name,
                    "revision": self.backend_revision,
                    "query": query,
                    **request,
                }
            )
            fingerprints[index] = fingerprint
            leader = leader_for_key.get(fingerprint)
            if leader is None:
                leader_for_key[fingerprint] = index
                leaders.append(index)
                continue
            self.providers.record_deduplicated_request(
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
                backend_name,
                backend,
                queries,
                leaders,
                fingerprints,
                limit=limit,
                offset=offset,
                domains=domains,
                request=request,
                concurrency=min(max(int(params.get("concurrency", 5)), 1), 20),
            )
        elif leaders and backend_name == "local" and isinstance(backend, BatchSearchBackend):
            leader_rows = await self._search_many_batched(
                state,
                backend,
                queries,
                leaders,
                limit=limit,
                offset=offset,
                domains=domains,
                request=request,
            )
        else:
            gate = asyncio.Semaphore(min(max(int(params.get("concurrency", 5)), 1), 20))

            async def one(index: int) -> SearchBatch:
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
                        return SearchBatch(
                            query=queries[index],
                            failure=self.providers.provider_failure(exc),
                        )
                    return SearchBatch(
                        query=queries[index],
                        hits=hits,
                    )

            returned = await asyncio.gather(*(one(index) for index in leaders))
            leader_rows = dict(zip(leaders, returned, strict=True))

        for index in leaders:
            batches[index] = leader_rows[index]
        for index, fingerprint in fingerprints.items():
            if index in leader_rows:
                continue
            leader = leader_for_key[fingerprint]
            duplicate = copy.deepcopy(leader_rows[leader])
            duplicate.query = queries[index]
            batches[index] = duplicate

        assert all(batch is not None for batch in batches)
        finalized = [batch for batch in batches if batch is not None]
        for index, batch in enumerate(finalized):
            if batch.failure is None:
                self._record_search_hits(
                    state,
                    list(batch.hits),
                    query_index=index,
                )

        provider_failures = [
            batch.failure.model_dump(mode="json")
            for index, batch in enumerate(finalized)
            if index in fingerprints and batch.failure is not None
        ]
        if (
            provider_failures
            and len(provider_failures) == len(fingerprints)
            and all(
                self.providers.is_systemic_search_failure(failure) for failure in provider_failures
            )
        ):
            attempts = len(_provider_attempts())
            raise CapabilityProviderError.from_failures(
                provider_failures,
                attempts=attempts,
            )
        payloads = [batch.model_dump(mode="json") for batch in finalized]
        for payload, batch in zip(payloads, finalized, strict=True):
            if batch.failure is None:
                payload["hits"] = [self._search_hit_wire(hit) for hit in batch.hits]
        return payloads

    async def _search_many_coalesced(
        self,
        state: BrokerSession,
        backend_name: str,
        backend: SearchBackend,
        queries: list[str],
        leaders: list[int],
        fingerprints: dict[int, str],
        *,
        limit: int,
        offset: int,
        domains: list[str] | None,
        request: dict[str, Any],
        concurrency: int,
    ) -> dict[int, SearchBatch]:
        """Attach active keys and send the remaining local keys as one batch."""

        operation = f"{backend_name}.search"
        key_for_index = {
            index: self.providers.flight_key(operation, fingerprints[index]) for index in leaders
        }
        admission = await self.providers.admit_flights(
            state,
            {key_for_index[index]: (fingerprints[index], [index]) for index in leaders},
            group_new=backend_name == "local" and isinstance(backend, BatchSearchBackend),
        )
        index_for_key = {key: index for index, key in key_for_index.items()}
        gate = asyncio.Semaphore(concurrency)
        for group in admission.new_groups:
            group_indexes = [index_for_key[key] for key in group.keys]
            group_indexes.sort()
            if backend_name == "local" and isinstance(backend, BatchSearchBackend):

                async def execute_local(
                    group: FlightGroup = group,
                    group_indexes: list[int] = group_indexes,
                ) -> dict[str, SearchBatch]:
                    rows = await self._search_many_batched(
                        state,
                        backend,
                        queries,
                        group_indexes,
                        limit=limit,
                        offset=offset,
                        domains=domains,
                        request=request,
                        operation_id=group.operation_id,
                        track_execution=False,
                    )
                    return {key_for_index[index]: row for index, row in rows.items()}

                self.providers.start_flight_group(state, group, execute_local)
                continue

            if len(group_indexes) != 1:
                raise RuntimeError("non-batch search flight contains multiple keys")
            index = group_indexes[0]
            key = key_for_index[index]

            async def execute_one(
                group: FlightGroup = group,
                index: int = index,
                key: str = key,
            ) -> dict[str, SearchBatch]:
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
                            operation_id=group.operation_id,
                            track_execution=False,
                        )
                    except ProviderRequestError as exc:
                        row = SearchBatch(
                            query=queries[index],
                            failure=self.providers.provider_failure(exc),
                        )
                    else:
                        row = SearchBatch(
                            query=queries[index],
                            hits=hits,
                        )
                return {key: row}

            self.providers.start_flight_group(state, group, execute_one)

        returned = await asyncio.gather(
            *(
                self.providers.await_flight(state, admission.waiters[key_for_index[index]])
                for index in leaders
            )
        )
        rows: dict[int, SearchBatch] = {}
        for index, result in zip(leaders, returned, strict=True):
            row = SearchBatch.model_validate(result)
            # Whitespace-equivalent calls share one provider request but retain
            # the exact query string at each caller's public boundary.
            row.query = queries[index]
            rows[index] = row
        return rows

    async def _search_many_batched(
        self,
        state: BrokerSession,
        backend: BatchSearchBackend,
        queries: list[str],
        leaders: list[int],
        *,
        limit: int,
        offset: int,
        domains: list[str] | None,
        request: dict[str, Any],
        operation_id: str | None = None,
        track_execution: bool = True,
    ) -> dict[int, SearchBatch]:
        """Execute unique local queries as one real provider microbatch."""

        backend_name, resolved = self._search_backend(state)
        if resolved is not backend:
            raise RuntimeError("Session search backend changed during batch execution.")
        unique_queries = [queries[index].strip() for index in leaders]
        operation_id = operation_id or f"op_{uuid.uuid4().hex}"

        async def execute() -> list[SearchBatch]:
            return await backend.search_many(
                unique_queries,
                limit=limit,
                offset=offset,
                domains=domains,
            )

        preflight = getattr(backend, "preflight_search", None)

        try:
            returned = await self.providers.run(
                state,
                backend=resolved,
                operation=f"{backend_name}.search",
                request_indexes=leaders,
                request_value={
                    "backend": backend_name,
                    "revision": self.backend_revision,
                    "queries": unique_queries,
                    **request,
                },
                request=execute,
                preflight=preflight if callable(preflight) else None,
                operation_id=operation_id,
                track_execution=track_execution,
            )
        except ProviderRequestError as exc:
            failure = self.providers.provider_failure(exc)
            return {
                index: SearchBatch(
                    query=queries[index],
                    failure=failure,
                )
                for index in leaders
            }
        if len(returned) != len(leaders):
            raise RuntimeError("Provider runtime accepted an invalid batch result count.")
        attempts = max(
            (
                record.attempt
                for record in _provider_attempts()
                if record.operation_id == operation_id
            ),
            default=1,
        )
        rows: dict[int, SearchBatch] = {}
        for index, batch in zip(leaders, returned, strict=True):
            failure = batch.failure
            if failure is not None:
                failure = failure.model_copy(update={"attempts": attempts})
                if failure.code == "provider_rejected":
                    failure = failure.model_copy(
                        update={"message": "Provider rejected one search item."}
                    )
            rows[index] = SearchBatch(
                query=queries[index],
                hits=list(batch.hits) if failure is None else [],
                failure=failure,
            )
        if any(row.failure is not None for row in rows.values()):
            for record in reversed(_provider_attempts()):
                if record.operation_id == operation_id and record.status == "success":
                    record.status = "partial"
                    break
        return rows
