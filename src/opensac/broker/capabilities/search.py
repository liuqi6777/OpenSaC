from __future__ import annotations

from typing import TYPE_CHECKING, Any, Self

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from opensac.backends.search import SearchBackend, SearchHit
from opensac.broker._utils import (
    document_handle_for_hit,
    document_identity,
    optional_string_list,
    source_for,
    string,
)
from opensac.broker.call_context import current_call, current_provider_attempts
from opensac.broker.failures import CapabilityFailure
from opensac.broker.registry import BaseCapabilities, CapabilityRequest, capability_method
from opensac.broker.session import BrokerSession, FlightGroup
from opensac.provider import ProviderRequestError
from opensac.tracing import HitRecord

from ..providers.execution import BackendBinding, CapabilityProviderError, ProviderExecutor

if TYPE_CHECKING:
    from opensac.broker.capabilities.catalog import CapabilityBuildContext

type _SearchOutcome = list[SearchHit] | CapabilityFailure

_SEARCH_HITS = TypeAdapter(list[SearchHit])


class SearchLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    max_queries_per_request: int = Field(default=64, ge=1)
    max_query_chars: int = Field(default=4_096, ge=1)
    max_top_k: int = Field(default=600, ge=1)
    max_limit: int = Field(default=100, ge=1)
    max_offset: int = Field(default=500, ge=0)
    max_concurrency: int = Field(default=20, ge=1)


class SearchQueryRequest(CapabilityRequest):
    query: str = ""
    include_domains: list[str] | None = None
    limit: int = 10
    offset: int = 0

    @field_validator("include_domains", mode="before")
    @classmethod
    def validate_domains(cls, value: Any) -> Any:
        if value is not None and not isinstance(value, list):
            raise ValueError("include_domains must be a list of strings")
        return value


class SearchCapabilities(BaseCapabilities):
    """Implement search capabilities against the session's admitted backend."""

    name = "search"
    available = True

    def __init__(
        self,
        providers: ProviderExecutor,
        bindings: dict[str, BackendBinding[SearchBackend]],
        *,
        limits: SearchLimits,
    ) -> None:
        if not bindings:
            raise ValueError("at least one search backend must be configured")
        self.providers = providers
        self.bindings = bindings
        self.limits = limits
        self.max_search_query_chars = limits.max_query_chars
        self.max_search_top_k = limits.max_top_k
        self.inflight_coalescing = providers.flights.enabled

    @classmethod
    def from_context(cls, context: CapabilityBuildContext) -> Self:
        return cls(
            context.providers,
            context.search_bindings,
            limits=context.config.search,
        )

    def _query_trace(self, request: SearchQueryRequest) -> list[str]:
        query = request.query
        return [query[: self.max_search_query_chars]] if query else []

    def manifest(self, *, backend_name: str) -> dict[str, Any]:
        try:
            binding = self.bindings[backend_name]
        except KeyError as exc:
            raise ValueError(f"Backend {backend_name!r} is not configured for search") from exc
        return {
            "backend": backend_name,
            "supports_include_domains": binding.backend.supports_domains,
            "max_depth": binding.backend.max_depth,
            "limits": {
                "max_queries_per_request": self.limits.max_queries_per_request,
                "max_query_chars": self.limits.max_query_chars,
                "max_top_k": self.limits.max_top_k,
                "max_limit": self.limits.max_limit,
                "max_offset": self.limits.max_offset,
                "max_concurrency": self.limits.max_concurrency,
            },
        }

    def _search_binding(
        self,
        state: BrokerSession,
    ) -> tuple[str, BackendBinding[SearchBackend]]:
        """The one backend binding this session searches.

        Resolved from the session rather than from the method name, which is
        what makes `search.query` backend-neutral. The deployment records its
        one configured backend on every session, so there is nothing to choose
        between here; a session that somehow holds two is a bug worth stopping
        on rather than a tie to break arbitrarily.
        """
        names = sorted(state.policy.allowed_backends & set(self.bindings))
        if len(names) != 1:
            raise RuntimeError(
                "A session must have exactly one configured search backend, "
                f"this one has {names or sorted(state.policy.allowed_backends)}."
            )
        return names[0], self.bindings[names[0]]

    @capability_method("search.query", SearchQueryRequest, trace_queries="_query_trace")
    async def query(
        self,
        state: BrokerSession,
        request: SearchQueryRequest,
    ) -> list[dict[str, Any]]:
        return await self._search(state, request)

    def _prepare_search(
        self,
        state: BrokerSession,
        request: SearchQueryRequest,
    ) -> tuple[str, BackendBinding[SearchBackend], str, list[str] | None, int, int]:
        """Validate one query without executing it or charging usage."""
        backend_name, binding = self._search_binding(state)
        state.policy.require_backend(backend_name)
        query = string(
            request.query,
            "query",
            strip=True,
            max_chars=self.max_search_query_chars,
        )
        domains = optional_string_list(
            request.include_domains,
            "include_domains",
        )
        # Refused rather than dropped. A backend-neutral method name is only
        # honest if a parameter it cannot honour fails loudly: a program that
        # asked for one site and silently got the whole web draws exactly the
        # wrong conclusion from an empty result.
        if domains and not binding.backend.supports_domains:
            raise ValueError(
                f"The '{backend_name}' backend has no domain filter, so "
                f"include_domains={list(domains)!r} cannot be honoured. Drop the argument "
                "and filter the hits in Python, or put the constraint in the query."
            )
        limit, offset = self._search_window(request)
        # Same rule as `domains`, for the other thing a backend cannot honour.
        # Clipping would let a program believe it read rank 150 and conclude the
        # document is absent, when nothing ever looked past the ceiling.
        depth = offset + limit
        if binding.backend.max_depth is not None and depth > binding.backend.max_depth:
            raise ValueError(
                f"The '{backend_name}' backend reaches rank {binding.backend.max_depth} at "
                f"most, and offset={offset} with limit={limit} asks for {depth}. "
                "Narrow the window, or find the document with a different query."
            )
        return backend_name, binding, query, domains, limit, offset

    @staticmethod
    def _request_value(
        binding: BackendBinding[SearchBackend],
        query: str,
        *,
        limit: int,
        offset: int,
        domains: list[str] | None,
    ) -> dict[str, object]:
        return {
            "backend": binding.route,
            "revision": binding.revision,
            "query": query,
            "limit": limit,
            "offset": offset,
            "domains": domains,
        }

    def _search_window(
        self,
        request: SearchQueryRequest,
    ) -> tuple[int, int]:
        """Validate a requested retrieval window before clipping or fan-out."""
        limit = request.limit
        offset = request.offset
        if not 1 <= limit <= self.limits.max_limit:
            raise ValueError(f"limit must be between 1 and {self.limits.max_limit}")
        if not 0 <= offset <= self.limits.max_offset:
            raise ValueError(f"offset must be between 0 and {self.limits.max_offset}")
        depth = offset + limit
        if depth > self.max_search_top_k:
            raise ValueError(
                f"offset={offset} with limit={limit} asks for retrieval "
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
        request: SearchQueryRequest,
        *,
        request_index: int = 0,
        record_usage: bool = True,
        request_id: str | None = None,
        track_execution: bool = True,
    ) -> list[SearchHit]:
        """Execute one search without mutating the session reference table."""
        _backend_name, binding, query, domains, limit, offset = self._prepare_search(state, request)
        if record_usage:
            await state.policy.record_search()

        async def search(backend: SearchBackend) -> list[SearchHit]:
            return _SEARCH_HITS.validate_python(
                await backend.search(
                    query,
                    limit=limit,
                    offset=offset,
                    domains=domains,
                ),
                strict=True,
            )

        preflight = getattr(binding.backend, "preflight_search", None)
        return await self.providers.execute(
            state,
            binding,
            request_indexes=[request_index],
            request_value=self._request_value(
                binding,
                query,
                limit=limit,
                offset=offset,
                domains=domains,
            ),
            request=search,
            preflight=preflight if callable(preflight) else None,
            request_id=request_id,
            track_execution=track_execution,
        )

    async def _search(
        self,
        state: BrokerSession,
        request: SearchQueryRequest,
    ) -> list[dict[str, Any]]:
        if self.inflight_coalescing:
            return await self._search_coalesced(state, request)
        hits = await self._retrieve_search(state, request)
        return self._record_search_hits(state, hits)

    async def _search_coalesced(
        self,
        state: BrokerSession,
        request: SearchQueryRequest,
    ) -> list[dict[str, Any]]:
        _backend_name, binding, query, domains, limit, offset = self._prepare_search(state, request)
        await state.policy.record_search()
        normalized_domains = sorted(set(domains)) if domains else None
        fingerprint = self.providers.fingerprint(
            self._request_value(
                binding,
                query,
                limit=limit,
                offset=offset,
                domains=normalized_domains,
            )
        )
        key = self.providers.flights.key(binding.namespace, fingerprint)
        admission = await self.providers.flights.admit(
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
                        SearchQueryRequest(
                            query=query,
                            limit=limit,
                            offset=offset,
                            include_domains=normalized_domains,
                        ),
                        record_usage=False,
                        request_id=group.request_id,
                        track_execution=False,
                    )
                except ProviderRequestError as exc:
                    outcome: _SearchOutcome = CapabilityFailure.model_validate(
                        self.providers.provider_failure(exc)
                    )
                else:
                    outcome = hits
                return {key: outcome}

            self.providers.flights.start(state, group, execute)

        outcome = await self.providers.flights.wait(state, admission.waiters[key])
        if isinstance(outcome, CapabilityFailure):
            raise CapabilityProviderError.from_failure(
                outcome.model_dump(mode="json"),
                attempts=len(current_provider_attempts()),
            )
        if not isinstance(outcome, list) or not all(isinstance(hit, SearchHit) for hit in outcome):
            raise RuntimeError("Search flight returned an invalid outcome.")
        return self._record_search_hits(state, outcome)
