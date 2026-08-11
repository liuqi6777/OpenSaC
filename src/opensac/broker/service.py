from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from jsonschema import Draft202012Validator
from openai import AsyncOpenAI
from opensac_sdk.models import ContentSnippet, SearchBatch, SearchHit

from opensac.backends.base import BatchSearchBackend, ClosableSearchBackend, SearchBackend
from opensac.broker.policy import CapabilityPolicy, MechanismDisabled
from opensac.metrics import CapacityGate
from opensac.models import (
    CAPABILITY_METHODS,
    CapabilityEvent,
    CoalescedRequestRecord,
    DeduplicatedRequestRecord,
    EvidenceTraceRecord,
    HitRecord,
    Mechanisms,
    ModelAttemptRecord,
    ProviderAttemptRecord,
    Session,
)
from opensac.provider import (
    ProviderAttempt,
    ProviderPolicy,
    ProviderRequestError,
    ProviderRuntime,
    ProviderWait,
)


@dataclass
class _ProviderTraceBuffer:
    """Mutable attempt sink shared with a leader's detached transport task."""

    records: list[ProviderAttemptRecord] = field(default_factory=list)
    event: CapabilityEvent | None = None

    def append(self, record: ProviderAttemptRecord) -> None:
        self.records.append(record)
        if self.event is not None:
            self.event.provider_attempts.append(record)

    def bind(self, event: CapabilityEvent) -> None:
        self.event = event


_EVENT_MODEL_TOKENS: ContextVar[int] = ContextVar(
    "opensac_event_model_tokens", default=0
)
# Hits minted while serving the capability currently on the stack. `_search_many`
# calls `_search` directly rather than re-entering `call`, so a fan-out
# accumulates into the one event that represents it, which is what makes
# per-query duplication measurable.
#
# Defaults to None rather than to a list: a mutable default is shared by every
# context that never calls `.set()`, so a search reached outside `call` would
# append to one process-global list that nothing ever drains.
_EVENT_HITS: ContextVar[list[HitRecord] | None] = ContextVar(
    "opensac_event_hits", default=None
)
_EVENT_MODEL_ATTEMPTS: ContextVar[list[ModelAttemptRecord] | None] = ContextVar(
    "opensac_event_model_attempts", default=None
)
_EVENT_EVIDENCE: ContextVar[list[EvidenceTraceRecord] | None] = ContextVar(
    "opensac_event_evidence", default=None
)
_EVENT_PROVIDER_ATTEMPTS: ContextVar[list[ProviderAttemptRecord] | None] = ContextVar(
    "opensac_event_provider_attempts", default=None
)
_EVENT_PROVIDER_TRACE: ContextVar[_ProviderTraceBuffer | None] = ContextVar(
    "opensac_event_provider_trace", default=None
)
_EVENT_DEDUPLICATED: ContextVar[list[DeduplicatedRequestRecord] | None] = ContextVar(
    "opensac_event_deduplicated", default=None
)
_EVENT_COALESCED: ContextVar[list[CoalescedRequestRecord] | None] = ContextVar(
    "opensac_event_coalesced", default=None
)
_EVENT_SESSION_TOKEN: ContextVar[str | None] = ContextVar(
    "opensac_event_session_token", default=None
)
_EVENT_EXECUTION_ID: ContextVar[str | None] = ContextVar(
    "opensac_event_execution_id", default=None
)

# Query parameters that identify a referrer rather than a document. Stripping
# them keeps two links to the same page from being counted as two documents.
# Deliberately a short, conservative list: a parameter wrongly stripped merges
# pages that are genuinely different, which is the worse error.
_TRACKING_PARAMS = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "utm_id",
        "gclid",
        "fbclid",
        "msclkid",
        "mc_cid",
        "mc_eid",
        "ref_src",
        "spm",
    }
)


class ExtractionInfrastructureError(RuntimeError):
    """Every item failed before the provider produced an extraction output."""

    code = "extraction_provider_unavailable"
    retryable = True

    def __init__(self) -> None:
        # Provider exception strings may contain response bodies. The public
        # error deliberately carries only a stable, actionable classification.
        super().__init__("The extraction provider failed for every item; retry the call.")


class CapabilityProviderError(RuntimeError):
    """A provider failure promoted from item rows to the capability RPC."""

    def __init__(
        self,
        *,
        code: str,
        message: str,
        retryable: bool,
        attempts: int,
        provider_status: int | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.attempts = max(int(attempts), 0)
        self.provider_status = provider_status
        self.retry_after_seconds = retry_after_seconds

    @classmethod
    def from_failure(
        cls,
        failure: dict[str, Any],
        *,
        attempts: int | None = None,
    ) -> CapabilityProviderError:
        return cls(
            code=str(failure.get("code") or "provider_invalid_response"),
            message=str(failure.get("message") or "Provider operation failed."),
            retryable=bool(failure.get("retryable")),
            attempts=(
                int(failure.get("attempts") or 0) if attempts is None else attempts
            ),
            provider_status=failure.get("provider_status"),
            retry_after_seconds=failure.get("retry_after_seconds"),
        )

    @classmethod
    def from_failures(
        cls,
        failures: list[dict[str, Any]],
        *,
        attempts: int,
    ) -> CapabilityProviderError:
        """Promote a whole failed batch without overstating retryability."""

        if not failures:
            raise ValueError("at least one provider failure is required")
        retryable = all(bool(failure.get("retryable")) for failure in failures)
        representative = next(
            (
                failure
                for failure in failures
                if not bool(failure.get("retryable"))
            ),
            failures[0],
        )
        aggregate = dict(representative)
        aggregate["retryable"] = retryable
        if retryable:
            retry_after = [
                float(value)
                for failure in failures
                if (value := failure.get("retry_after_seconds")) is not None
            ]
            if retry_after:
                aggregate["retry_after_seconds"] = max(retry_after)
        return cls.from_failure(aggregate, attempts=attempts)


class InflightCapacityError(RuntimeError):
    code = "inflight_capacity_exhausted"
    retryable = True
    attempts = 0
    provider_status = None
    retry_after_seconds = None

    def __init__(self) -> None:
        super().__init__(
            "The session in-flight provider registry is full; retry the capability call."
        )


@dataclass(frozen=True)
class EvidenceRecord:
    ref: str
    kind: str
    text: str
    coordinates: dict[str, Any]
    document_fingerprint: str
    passage_fingerprint: str


@dataclass
class _FlightGroup:
    """Transport work shared by one or more active provider request keys."""

    operation_id: str
    keys: set[str] = field(default_factory=set)
    entries: dict[str, _FlightEntry] = field(default_factory=dict)
    task: asyncio.Task[None] | None = None


@dataclass
class _FlightEntry:
    """One active, session-scoped provider result shared by capability calls."""

    future: asyncio.Future[Any]
    operation_id: str
    request_fingerprint: str
    group: _FlightGroup
    waiters: int = 0


@dataclass(eq=False)
class _FlightWaiter:
    """One capability call's attachment to one unique provider request key."""

    key: str
    entry: _FlightEntry
    execution_id: str | None
    active: bool = True


@dataclass
class _FlightAdmission:
    waiters: dict[str, _FlightWaiter]
    new_groups: list[_FlightGroup]


@dataclass(frozen=True)
class _ExtractionError:
    code: str
    message: str
    retryable: bool = False

    def wire(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }


@dataclass(frozen=True)
class _ModelOutput:
    content: str | None
    tokens: int
    duration_seconds: float
    provider_failed: bool = False


@dataclass
class BrokerSession:
    session: Session
    policy: CapabilityPolicy
    references: dict[str, SearchHit] = field(default_factory=dict)
    # Secondary indexes over the same hits, so a program can address a document
    # by the identifier it can actually see and re-type. A ref is opaque by
    # design -- the program must not be able to construct one -- but opacity and
    # transcribability are different requirements, and encoding both into one
    # string served neither: the handle has to be unguessable for the broker and
    # short and reliable for the model.
    #
    # This does not widen what a program may reach. Admission is still "this
    # session searched it up": a docid never returned by a search is absent from
    # these tables and still raises. Only the lookup key changed, so backend
    # routing, `require_backend`, and citation provenance all keep working off
    # the same stored `SearchHit`.
    by_docid: dict[str, SearchHit] = field(default_factory=dict)
    by_url: dict[str, SearchHit] = field(default_factory=dict)
    # Document text already retrieved in this session, keyed by ref. Scoped to
    # the session because that is the rollout: the pool a program builds is the
    # thing it reads repeatedly, and nothing about one question's reading should
    # reach another's.
    #
    # Only successful fetches are stored. Caching a failure would freeze a
    # transient timeout for the rest of the rollout, and re-reading a page that
    # failed once is exactly what a program should be allowed to do.
    content_cache: dict[str, dict[str, Any]] = field(default_factory=dict)
    content_cache_bytes: int = 0
    evidence: dict[str, EvidenceRecord] = field(default_factory=dict)
    evidence_passage_bytes: int = 0
    flights: dict[str, _FlightEntry] = field(default_factory=dict)
    flight_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    flight_waiters_by_execution: dict[str, set[_FlightWaiter]] = field(
        default_factory=dict
    )
    traces: dict[str, list[CapabilityEvent]] = field(default_factory=dict)
    trace_sequence: int = 0

    @property
    def mechanisms(self) -> Mechanisms:
        return self.session.mechanisms

    def next_trace_sequence(self) -> int:
        self.trace_sequence += 1
        return self.trace_sequence

    def cache_content(self, row: dict[str, Any], budget: int) -> None:
        """Keep a fetched document for the rest of the session, within budget.

        Past the budget nothing is evicted and nothing new is stored: the
        rollout degrades to fetching every time, which is merely the old
        behaviour, rather than to a cache that spends its time thrashing.
        """
        ref = str(row.get("ref") or "")
        text = row.get("text") or ""
        if not ref or ref in self.content_cache or row.get("failure") is not None:
            return
        size = len(str(text).encode("utf-8"))
        if self.content_cache_bytes + size > budget:
            return
        self.content_cache[ref] = row
        self.content_cache_bytes += size

    def remember(self, hit: SearchHit) -> None:
        """Index one hit under every handle it can be reached by.

        ``setdefault`` throughout: first sighting wins. The stored hit is only
        used to reach content and to resolve a citation, and both are properties
        of the document rather than of the query that surfaced it, so keeping
        the first makes the tables independent of the order queries happened to
        complete. The dict returned to the program still carries this query's
        own rank and snippet.
        """
        self.references.setdefault(hit.ref, hit)
        if hit.docid:
            self.by_docid.setdefault(str(hit.docid), hit)
        if hit.url:
            self.by_url.setdefault(hit.url, hit)


class BrokerService:
    def __init__(
        self,
        backends: dict[str, SearchBackend],
        *,
        model_client: AsyncOpenAI | None = None,
        extraction_model: str = "",
        max_concurrency: int = 12,
        max_context_payload_bytes: int = 200_000,
        session_content_cache_bytes: int = 32_000_000,
        max_search_queries_per_request: int = 64,
        max_search_query_chars: int = 4096,
        max_search_top_k: int = 600,
        max_extract_items: int = 256,
        max_extract_instruction_bytes: int = 16_384,
        max_extract_schema_bytes: int = 65_536,
        max_extract_item_bytes: int = 65_536,
        max_extract_total_item_bytes: int = 2_097_152,
        max_extract_schema_depth: int = 8,
        max_extract_repair_attempts: int = 1,
        max_evidence_chars: int = 16_000,
        max_evidence_records: int = 4_096,
        max_evidence_passage_bytes: int = 33_554_432,
        max_content_refs_per_request: int = 256,
        inflight_coalescing: bool = False,
        max_inflight_keys: int = 256,
        max_waiters_per_flight: int = 64,
        provider_runtime: ProviderRuntime | None = None,
        backend_revision: str = "",
    ) -> None:
        self.backends = backends
        self.model_client = model_client
        self.extraction_model = extraction_model
        self.sessions: dict[str, BrokerSession] = {}
        self.execution_tasks: dict[tuple[str, str], set[asyncio.Task[Any]]] = {}
        self.capacity_gate = CapacityGate(max_concurrency)
        self.max_context_payload_bytes = max_context_payload_bytes
        self.session_content_cache_bytes = session_content_cache_bytes
        retrieval_limits = {
            "max_search_queries_per_request": max_search_queries_per_request,
            "max_search_query_chars": max_search_query_chars,
            "max_search_top_k": max_search_top_k,
        }
        for name, value in retrieval_limits.items():
            if int(value) < 1:
                raise ValueError(f"{name} must be at least 1")
        self.max_search_queries_per_request = int(max_search_queries_per_request)
        self.max_search_query_chars = int(max_search_query_chars)
        self.max_search_top_k = int(max_search_top_k)
        extraction_limits = {
            "max_extract_items": max_extract_items,
            "max_extract_instruction_bytes": max_extract_instruction_bytes,
            "max_extract_schema_bytes": max_extract_schema_bytes,
            "max_extract_item_bytes": max_extract_item_bytes,
            "max_extract_total_item_bytes": max_extract_total_item_bytes,
            "max_extract_schema_depth": max_extract_schema_depth,
            "max_evidence_chars": max_evidence_chars,
            "max_evidence_records": max_evidence_records,
            "max_evidence_passage_bytes": max_evidence_passage_bytes,
            "max_content_refs_per_request": max_content_refs_per_request,
            "max_inflight_keys": max_inflight_keys,
            "max_waiters_per_flight": max_waiters_per_flight,
        }
        for name, value in extraction_limits.items():
            if int(value) < 1:
                raise ValueError(f"{name} must be at least 1")
            setattr(self, name, int(value))
        if int(max_extract_repair_attempts) not in {0, 1}:
            raise ValueError("max_extract_repair_attempts must be 0 or 1")
        self.max_extract_repair_attempts = int(max_extract_repair_attempts)
        self.inflight_coalescing = bool(inflight_coalescing)
        self.provider_runtime = provider_runtime or ProviderRuntime(
            {
                "local.search": ProviderPolicy(concurrency=max_concurrency),
                "web.search": ProviderPolicy(concurrency=max_concurrency),
                "local.document": ProviderPolicy(concurrency=6),
                "web.scrape": ProviderPolicy(concurrency=6),
            }
        )
        self.backend_revision = backend_revision

    async def aclose(self) -> None:
        """Close backend-owned connection pools once the broker stops."""
        await asyncio.gather(
            *(self._cancel_all_flights(state) for state in self.sessions.values())
        )
        pending = {
            task
            for tasks in self.execution_tasks.values()
            for task in tasks
            if not task.done()
        }
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self.execution_tasks.clear()
        closable: list[ClosableSearchBackend] = []
        seen: set[int] = set()
        for backend in self.backends.values():
            if id(backend) in seen or not isinstance(backend, ClosableSearchBackend):
                continue
            seen.add(id(backend))
            closable.append(backend)
        await asyncio.gather(*(backend.aclose() for backend in closable))

    def register_session(self, session: Session, *, token: str | None = None) -> BrokerSession:
        state = BrokerSession(
            session=session,
            policy=CapabilityPolicy(
                set(session.backends),
                usage=session.usage,
                budget=session.budget,
                terminal_reason=session.terminal_reason,
            ),
        )
        self.sessions[token or session.token] = state
        return state

    def unregister_session(self, token: str) -> None:
        self.sessions.pop(token, None)

    def _track_execution_task(
        self,
        token: str,
        execution_id: str | None,
        task: asyncio.Task[Any],
    ) -> None:
        if not execution_id:
            return
        key = (token, execution_id)
        tasks = self.execution_tasks.setdefault(key, set())
        tasks.add(task)

        def finished(done: asyncio.Task[Any]) -> None:
            current = self.execution_tasks.get(key)
            if current is None:
                return
            current.discard(done)
            if not current:
                self.execution_tasks.pop(key, None)

        task.add_done_callback(finished)

    async def cancel_execution(
        self,
        token: str,
        execution_id: str,
        reason: str = "provider_cancelled",
    ) -> int:
        """Cancel and drain provider work owned by one sandbox execution."""

        del reason
        state = self.sessions.get(token)
        if state is not None:
            async with state.flight_lock:
                flight_waiters = list(
                    state.flight_waiters_by_execution.get(execution_id, set())
                )
            await asyncio.gather(
                *(self._detach_flight_waiter(state, waiter) for waiter in flight_waiters)
            )
        key = (token, execution_id)
        tasks = set(self.execution_tasks.get(key, set()))
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.execution_tasks.pop(key, None)
        return len(tasks)

    async def cancel_session(self, token: str) -> int:
        state = self.sessions.get(token)
        cancelled_flights = 0
        if state is not None:
            cancelled_flights = await self._cancel_all_flights(state)
        keys = [key for key in self.execution_tasks if key[0] == token]
        return cancelled_flights + sum(
            await asyncio.gather(
                *(self.cancel_execution(*key) for key in keys),
            )
        )

    async def call(
        self,
        token: str,
        method: str,
        params: dict[str, Any],
        *,
        execution_id: str | None = None,
    ) -> Any:
        """Run one traced capability and make execution cancellation drain it."""

        if not execution_id:
            return await self._call_traced(
                token,
                method,
                params,
                execution_id=None,
            )
        task = asyncio.create_task(
            self._call_traced(
                token,
                method,
                params,
                execution_id=execution_id,
            )
        )
        self._track_execution_task(token, execution_id, task)
        try:
            return await task
        except asyncio.CancelledError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise

    async def _call_traced(
        self,
        token: str,
        method: str,
        params: dict[str, Any],
        *,
        execution_id: str | None = None,
    ) -> Any:
        state = self.sessions.get(token)
        if state is None:
            raise PermissionError("Unknown or expired session token")
        handlers: dict[str, Callable[[BrokerSession, dict[str, Any]], Awaitable[Any]]] = {
            "search.query": self._search_query,
            "search.query_many": self._search_query_many,
            "content.get_many": self._content_get_many,
            "content.snippets": self._content_snippets,
            "content.read": self._content_read,
            "content.grep": self._content_grep,
            "content.grep_report": self._content_grep_report,
            "citations.resolve": self._resolve_citations,
            "session.usage": self._session_usage,
            "llm.complete": self._complete,
            "llm.complete_many": self._complete_many,
            "llm.extract_many": self._extract_many,
        }
        assert set(handlers) == set(CAPABILITY_METHODS), (
            "The handler table and models.CAPABILITY_METHODS have diverged. "
            "CAPABILITY_METHODS drives the session manifest and therefore the "
            "skill text, so a capability added on one side only is either "
            "invisible to the model or advertised without an implementation."
        )
        handler = handlers.get(method)
        if handler is None:
            raise ValueError(f"Unsupported capability: {method}")
        sequence = state.next_trace_sequence()
        started = time.monotonic()
        token_context = _EVENT_MODEL_TOKENS.set(0)
        hits_context = _EVENT_HITS.set([])
        attempts_context = _EVENT_MODEL_ATTEMPTS.set([])
        evidence_context = _EVENT_EVIDENCE.set([])
        provider_trace = _ProviderTraceBuffer()
        provider_attempts_context = _EVENT_PROVIDER_ATTEMPTS.set(
            provider_trace.records
        )
        provider_trace_context = _EVENT_PROVIDER_TRACE.set(provider_trace)
        deduplicated_context = _EVENT_DEDUPLICATED.set([])
        coalesced_context = _EVENT_COALESCED.set([])
        session_token_context = _EVENT_SESSION_TOKEN.set(token)
        execution_context = _EVENT_EXECUTION_ID.set(execution_id)
        try:
            # Mechanism gates sit inside the traced region on purpose: an arm
            # that disables a capability wants to know how often the model kept
            # reaching for it, which is only visible if blocked calls are events.
            blocked = state.mechanisms.blocked_reason(method) or state.mechanisms.fanout_reason(
                method, params
            )
            if blocked:
                raise MechanismDisabled(blocked)
            result = await handler(state, params)
        except asyncio.CancelledError as exc:
            event = CapabilityEvent(
                sequence=sequence,
                method=method,
                status="cancelled",
                duration_seconds=time.monotonic() - started,
                queries=self._trace_queries(method, params),
                input_count=self._trace_input_count(method, params),
                hits=list(_EVENT_HITS.get() or []),
                model_tokens=_EVENT_MODEL_TOKENS.get(),
                model_attempts=list(_EVENT_MODEL_ATTEMPTS.get() or []),
                evidence_records=list(_EVENT_EVIDENCE.get() or []),
                provider_attempts=list(_EVENT_PROVIDER_ATTEMPTS.get() or []),
                deduplicated_requests=list(_EVENT_DEDUPLICATED.get() or []),
                coalesced_requests=list(_EVENT_COALESCED.get() or []),
                error_type=type(exc).__name__,
                error="Capability execution was cancelled.",
            )
            provider_trace.bind(event)
            self._append_trace(
                state,
                execution_id,
                event,
            )
            raise
        except Exception as exc:
            event = CapabilityEvent(
                sequence=sequence,
                method=method,
                status="error",
                duration_seconds=time.monotonic() - started,
                queries=self._trace_queries(method, params),
                input_count=self._trace_input_count(method, params),
                hits=list(_EVENT_HITS.get() or []),
                model_tokens=_EVENT_MODEL_TOKENS.get(),
                model_attempts=list(_EVENT_MODEL_ATTEMPTS.get() or []),
                evidence_records=list(_EVENT_EVIDENCE.get() or []),
                provider_attempts=list(_EVENT_PROVIDER_ATTEMPTS.get() or []),
                deduplicated_requests=list(_EVENT_DEDUPLICATED.get() or []),
                coalesced_requests=list(_EVENT_COALESCED.get() or []),
                error_type=type(exc).__name__,
                error=self._trace_error_message(exc),
            )
            provider_trace.bind(event)
            self._append_trace(
                state,
                execution_id,
                event,
            )
            raise
        else:
            payload, truncated = self._context_payload(state, result)
            event = CapabilityEvent(
                sequence=sequence,
                method=method,
                status="ok",
                duration_seconds=time.monotonic() - started,
                queries=self._trace_queries(method, params),
                input_count=self._trace_input_count(method, params),
                result_count=self._trace_result_count(method, result),
                hits=list(_EVENT_HITS.get() or []),
                model_tokens=_EVENT_MODEL_TOKENS.get(),
                model_attempts=list(_EVENT_MODEL_ATTEMPTS.get() or []),
                evidence_records=list(_EVENT_EVIDENCE.get() or []),
                provider_attempts=list(_EVENT_PROVIDER_ATTEMPTS.get() or []),
                deduplicated_requests=list(_EVENT_DEDUPLICATED.get() or []),
                coalesced_requests=list(_EVENT_COALESCED.get() or []),
                result_payload=payload,
                result_payload_truncated=truncated,
            )
            provider_trace.bind(event)
            self._append_trace(
                state,
                execution_id,
                event,
            )
            return result
        finally:
            _EVENT_MODEL_TOKENS.reset(token_context)
            _EVENT_HITS.reset(hits_context)
            _EVENT_MODEL_ATTEMPTS.reset(attempts_context)
            _EVENT_EVIDENCE.reset(evidence_context)
            _EVENT_PROVIDER_ATTEMPTS.reset(provider_attempts_context)
            _EVENT_PROVIDER_TRACE.reset(provider_trace_context)
            _EVENT_DEDUPLICATED.reset(deduplicated_context)
            _EVENT_COALESCED.reset(coalesced_context)
            _EVENT_SESSION_TOKEN.reset(session_token_context)
            _EVENT_EXECUTION_ID.reset(execution_context)

    def _context_payload(self, state: BrokerSession, result: Any) -> tuple[Any, bool]:
        """The result echoed back for a session that disables context decoupling.

        Returns ``(None, False)`` in every default run, so the trace a normal
        experiment writes is unchanged. When the switch is off the whole result
        goes back, capped only to keep one runaway page from breaking the RPC
        response; the cap is reported rather than hidden, because an arm that
        silently truncated would understate exactly the cost it exists to
        measure.
        """
        if state.mechanisms.context_decoupling:
            return None, False
        encoded = json.dumps(result, ensure_ascii=False, default=str)
        if len(encoded.encode("utf-8")) <= self.max_context_payload_bytes:
            return result, False
        return encoded[: self.max_context_payload_bytes], True

    @staticmethod
    def _append_trace(
        state: BrokerSession,
        execution_id: str | None,
        event: CapabilityEvent,
    ) -> None:
        if execution_id:
            state.traces.setdefault(execution_id, []).append(event)

    def take_trace(self, token: str, execution_id: str | None) -> list[CapabilityEvent]:
        if not execution_id:
            return []
        state = self.sessions.get(token)
        if state is None:
            return []
        return sorted(state.traces.pop(execution_id, []), key=lambda event: event.sequence)

    @staticmethod
    def _fingerprint(value: Any) -> str:
        """Versioned digest of a normalized, secret-free logical value."""

        def normalize(item: Any) -> Any:
            model_dump = getattr(item, "model_dump", None)
            if callable(model_dump):
                return model_dump(mode="json")
            if isinstance(item, set):
                return sorted(item, key=repr)
            return str(item)

        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=normalize,
        ).encode("utf-8")
        return "sha256:v1:" + hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _record_deduplicated_request(
        *,
        request_index: int,
        leader_index: int,
        request_fingerprint: str,
    ) -> None:
        records = _EVENT_DEDUPLICATED.get()
        if records is not None:
            records.append(
                DeduplicatedRequestRecord(
                    request_index=request_index,
                    leader_index=leader_index,
                    request_fingerprint=request_fingerprint,
                )
            )

    @staticmethod
    def _flight_key(operation: str, request_fingerprint: str) -> str:
        return f"{operation}:{request_fingerprint}"

    async def _admit_flights(
        self,
        state: BrokerSession,
        requests: dict[str, tuple[str, list[int]]],
        *,
        group_new: bool,
    ) -> _FlightAdmission:
        """Atomically attach or lead every unique key in one capability call.

        ``requests`` is already deduplicated within the call. Consequently one
        key consumes one waiter even if several logical rows map to it. The
        full validation happens under one lock before any entry or waiter is
        added, so a capacity error cannot leave a partial attachment behind.
        """

        if not self.inflight_coalescing or not requests:
            return _FlightAdmission(waiters={}, new_groups=[])

        execution_id = _EVENT_EXECUTION_ID.get()
        attached: list[tuple[str, _FlightEntry, str, list[int]]] = []
        created: list[tuple[str, _FlightEntry, str, list[int]]] = []
        async with state.flight_lock:
            # A completed result is never a cache. The group normally removes
            # itself before waking waiters; this defensive pruning closes the
            # narrow race where completion is waiting to acquire this lock.
            for key, entry in list(state.flights.items()):
                if entry.future.done():
                    state.flights.pop(key, None)

            new_keys: list[str] = []
            for key, (fingerprint, request_indexes) in requests.items():
                entry = state.flights.get(key)
                if entry is None:
                    new_keys.append(key)
                    continue
                if entry.waiters >= self.max_waiters_per_flight:
                    raise InflightCapacityError()
                attached.append((key, entry, fingerprint, request_indexes))

            if len(state.flights) + len(new_keys) > self.max_inflight_keys:
                raise InflightCapacityError()

            new_groups: list[_FlightGroup] = []
            shared_group: _FlightGroup | None = None
            if new_keys and group_new:
                shared_group = _FlightGroup(operation_id=f"op_{uuid.uuid4().hex}")
                new_groups.append(shared_group)
            for key in new_keys:
                fingerprint, request_indexes = requests[key]
                group = shared_group
                if group is None:
                    group = _FlightGroup(operation_id=f"op_{uuid.uuid4().hex}")
                    new_groups.append(group)
                entry = _FlightEntry(
                    future=asyncio.get_running_loop().create_future(),
                    operation_id=group.operation_id,
                    request_fingerprint=fingerprint,
                    group=group,
                )
                entry.future.add_done_callback(self._consume_flight_future)
                group.keys.add(key)
                group.entries[key] = entry
                state.flights[key] = entry
                created.append((key, entry, fingerprint, request_indexes))

            waiters: dict[str, _FlightWaiter] = {}
            for key, entry, _fingerprint, _request_indexes in [*attached, *created]:
                entry.waiters += 1
                waiter = _FlightWaiter(
                    key=key,
                    entry=entry,
                    execution_id=execution_id,
                )
                waiters[key] = waiter
                if execution_id:
                    state.flight_waiters_by_execution.setdefault(
                        execution_id, set()
                    ).add(waiter)

        if attached:
            state.policy.record_coalesced(len(attached))
            records = _EVENT_COALESCED.get()
            if records is not None:
                records.extend(
                    CoalescedRequestRecord(
                        operation_id=entry.operation_id,
                        request_indexes=list(request_indexes),
                        request_fingerprint=fingerprint,
                    )
                    for _key, entry, fingerprint, request_indexes in attached
                )
        return _FlightAdmission(waiters=waiters, new_groups=new_groups)

    def _start_flight_group(
        self,
        state: BrokerSession,
        group: _FlightGroup,
        execute: Callable[[], Awaitable[dict[str, Any]]],
    ) -> None:
        """Start one admitted transport group without yielding to another call."""

        if group.task is not None:
            raise RuntimeError("in-flight transport group was already started")

        async def run() -> None:
            results: dict[str, Any] | None = None
            failure: BaseException | None = None
            cancelled = False
            try:
                try:
                    results = await execute()
                    if set(results) != group.keys:
                        raise RuntimeError(
                            "in-flight transport group returned an invalid key set"
                        )
                except asyncio.CancelledError:
                    cancelled = True
                except BaseException as exc:
                    failure = exc
            finally:

                async def publish_and_cleanup() -> None:
                    # Remove before publishing the result: a call admitted after
                    # this point must lead a fresh transport rather than consume
                    # a completed value as an accidental cache.
                    async with state.flight_lock:
                        for key, entry in group.entries.items():
                            if state.flights.get(key) is entry:
                                state.flights.pop(key, None)

                    for key, entry in group.entries.items():
                        if entry.future.done():
                            continue
                        if cancelled:
                            entry.future.cancel()
                        elif failure is not None:
                            entry.future.set_exception(failure)
                        else:
                            assert results is not None
                            entry.future.set_result(results[key])

                cleanup = asyncio.create_task(publish_and_cleanup())
                while not cleanup.done():
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        # Repeated cancellation while the group is publishing
                        # must not strand a registry entry with an unresolved
                        # future. The cleanup task is independent and shielded.
                        cancelled = True
                await cleanup

        group.task = asyncio.create_task(run())

    @staticmethod
    def _consume_flight_future(future: asyncio.Future[Any]) -> None:
        """Suppress unobserved-exception warnings after every waiter detaches."""

        if not future.cancelled():
            future.exception()

    async def _await_flight(
        self,
        state: BrokerSession,
        waiter: _FlightWaiter,
    ) -> Any:
        try:
            # A waiter cancellation is only a detach. Shielding prevents it
            # from cancelling the shared future that other capability calls
            # are still waiting on.
            result = await asyncio.shield(waiter.entry.future)
            return copy.deepcopy(result)
        finally:
            await self._detach_flight_waiter(state, waiter)

    async def _detach_flight_waiter(
        self,
        state: BrokerSession,
        waiter: _FlightWaiter,
    ) -> None:
        cancel_task: asyncio.Task[None] | None = None
        async with state.flight_lock:
            if not waiter.active:
                return
            waiter.active = False
            entry = waiter.entry
            entry.waiters = max(0, entry.waiters - 1)
            if waiter.execution_id:
                execution_waiters = state.flight_waiters_by_execution.get(
                    waiter.execution_id
                )
                if execution_waiters is not None:
                    execution_waiters.discard(waiter)
                    if not execution_waiters:
                        state.flight_waiters_by_execution.pop(
                            waiter.execution_id, None
                        )
            group = entry.group
            task = group.task
            if (
                task is not None
                and not task.done()
                and all(item.waiters == 0 for item in group.entries.values())
            ):
                task.cancel()
                cancel_task = task
        if cancel_task is not None and cancel_task is not asyncio.current_task():
            await asyncio.gather(cancel_task, return_exceptions=True)

    async def _cancel_all_flights(self, state: BrokerSession) -> int:
        async with state.flight_lock:
            groups = {entry.group.operation_id: entry.group for entry in state.flights.values()}
            for group in groups.values():
                for entry in group.entries.values():
                    entry.waiters = 0
                    for waiter_set in state.flight_waiters_by_execution.values():
                        for waiter in waiter_set:
                            if waiter.entry is entry:
                                waiter.active = False
            state.flight_waiters_by_execution.clear()
            state.flights.clear()
            tasks = [
                group.task
                for group in groups.values()
                if group.task is not None and not group.task.done()
            ]
            for task in tasks:
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        return len(tasks)

    @staticmethod
    def _provider_failure(error: ProviderRequestError) -> dict[str, Any]:
        failure: dict[str, Any] = {
            "code": error.code,
            "message": error.message,
            "retryable": error.retryable,
            "attempts": error.attempts,
        }
        if error.provider_status is not None:
            failure["provider_status"] = error.provider_status
        if error.retry_after_seconds is not None:
            failure["retry_after_seconds"] = error.retry_after_seconds
        return failure

    @staticmethod
    def _is_systemic_search_failure(failure: dict[str, Any]) -> bool:
        return str(failure.get("code") or "") in {
            "provider_not_configured",
            "provider_timeout",
            "provider_rate_limited",
            "provider_unavailable",
            "provider_auth_failed",
            "provider_http_error",
            "provider_invalid_response",
        }

    @staticmethod
    def _is_systemic_content_failure(failure: dict[str, Any]) -> bool:
        """Whether every document failing means the content service is down.

        Permanent document and unexpected non-retryable HTTP failures remain
        aligned rows: unlike a shared outage, they may be specific to the URL
        or corpus entry the caller asked to fetch.
        """

        return str(failure.get("code") or "") in {
            "provider_timeout",
            "provider_rate_limited",
            "provider_unavailable",
            "provider_auth_failed",
            "provider_invalid_response",
        }

    async def _run_provider(
        self,
        state: BrokerSession,
        *,
        backend: SearchBackend,
        operation: str,
        request_indexes: list[int],
        request_value: Any,
        request: Callable[[], Awaitable[Any]],
        preflight: Callable[[], None] | None = None,
        operation_id: str | None = None,
        track_execution: bool = True,
    ) -> Any:
        """Execute, account and trace one real provider transport operation."""

        operation_id = operation_id or f"op_{uuid.uuid4().hex}"
        request_fingerprint = self._fingerprint(request_value)
        records = _EVENT_PROVIDER_ATTEMPTS.get()
        trace_buffer = _EVENT_PROVIDER_TRACE.get()

        def observe(attempt: ProviderAttempt) -> None:
            state.policy.record_provider_attempt(
                kind="search" if operation.endswith(".search") else "content",
                attempt=attempt.attempt,
            )
            if records is None:
                return
            record = ProviderAttemptRecord(
                operation_id=operation_id,
                attempt_id=f"{operation_id}:{attempt.attempt}",
                provider=operation.partition(".")[0],
                operation=operation,
                request_indexes=list(attempt.request_indexes),
                attempt=attempt.attempt,
                status=attempt.status,
                duration_seconds=attempt.duration_seconds,
                queue_seconds=attempt.queue_seconds,
                rate_limit_wait_seconds=attempt.rate_limit_wait_seconds,
                backoff_before_seconds=attempt.backoff_before_seconds,
                error_code=attempt.error_code,
                provider_status=attempt.provider_status,
                request_fingerprint=request_fingerprint,
            )
            if trace_buffer is not None:
                trace_buffer.append(record)
            else:
                records.append(record)

        def observe_wait(wait: ProviderWait) -> None:
            state.policy.record_provider_timing(
                phase=wait.phase,
                duration_seconds=wait.duration_seconds,
            )

        provider_identity = str(
            getattr(backend, "provider_identity", "") or f"{operation}:{id(backend)}"
        )
        task = asyncio.create_task(
            self.provider_runtime.run(
                operation,
                request,
                provider_identity=provider_identity,
                request_indexes=request_indexes,
                preflight=preflight,
                observer=observe,
                wait_observer=observe_wait,
            )
        )
        session_token = _EVENT_SESSION_TOKEN.get()
        if session_token and track_execution:
            self._track_execution_task(
                session_token,
                _EVENT_EXECUTION_ID.get(),
                task,
            )
        result = await task
        response_fingerprint = self._fingerprint(result)
        if records is not None:
            for record in reversed(records):
                if record.operation_id == operation_id and record.status == "success":
                    record.response_fingerprint = response_fingerprint
                    break
        return result

    def _trace_queries(self, method: str, params: dict[str, Any]) -> list[str]:
        if not method.startswith("search."):
            return []
        if method.endswith("_many"):
            raw_queries = params.get("queries", [])
            if not isinstance(raw_queries, list):
                return []
            return [
                str(item)[: self.max_search_query_chars]
                for item in raw_queries[: self.max_search_queries_per_request]
            ]
        query = str(params.get("query", ""))
        return [query[: self.max_search_query_chars]] if query else []

    @staticmethod
    def _trace_input_count(method: str, params: dict[str, Any]) -> int:
        if method.startswith("search."):
            return len(params.get("queries", [])) if method.endswith("_many") else 1
        if method == "citations.resolve" and "requests" in params:
            return len(params.get("requests", []))
        if method.startswith("content.") or method == "citations.resolve":
            return len(params.get("refs", []))
        if method in {"llm.complete_many", "llm.extract_many"}:
            key = "prompts" if method == "llm.complete_many" else "items"
            return len(params.get(key, []))
        return 1

    @staticmethod
    def _trace_result_count(method: str, result: Any) -> int:
        if method.startswith("search.") and method.endswith("_many"):
            return sum(len(batch.get("hits", [])) for batch in result)
        if isinstance(result, list):
            return len(result)
        if method == "content.grep_report" and isinstance(result, dict):
            return len(result.get("matches", []))
        return 1 if result is not None else 0

    # A failed event without its message is a category, not a diagnosis. The
    # type alone collapses cases whose remedies differ completely: `ValueError`
    # covers an unrecognised handle, an empty grep pattern and a depth beyond
    # what the backend serves, and only the text says which one happened.
    #
    # Bounded because the trace is written for every run and has to stay
    # publishable, and an exception is the one field here whose content is not
    # under this module's control -- a backend is free to put a response body in
    # it. Addresses and queries are already recorded verbatim, so the bound is
    # about volume rather than secrecy; the head is kept because that is where
    # the exception says what it is.
    _ERROR_MESSAGE_CHARS = 400

    @classmethod
    def _trace_error_message(cls, exc: BaseException) -> str | None:
        message = str(exc).strip()
        if not message:
            # `raise ValueError` with no argument is a real case, and an empty
            # string in the record would read as "the message was dropped".
            return None
        if len(message) <= cls._ERROR_MESSAGE_CHARS:
            return message
        return message[: cls._ERROR_MESSAGE_CHARS] + "... [truncated]"

    def _search_backend(self, state: BrokerSession) -> tuple[str, SearchBackend]:
        """The one backend this session searches.

        Resolved from the session rather than from the method name, which is
        what makes `search.query` backend-neutral. `create_session` admits
        exactly one, so there is nothing to choose between here; a session that
        somehow holds two is a bug worth stopping on rather than a tie to break
        arbitrarily.
        """
        names = sorted(state.policy.allowed_backends & set(self.backends))
        if len(names) != 1:
            raise RuntimeError(
                "A session must have exactly one configured search backend, "
                f"this one has {names or sorted(state.policy.allowed_backends)}."
            )
        return names[0], self.backends[names[0]]

    async def _search_query(
        self, state: BrokerSession, params: dict[str, Any]
    ) -> list[dict[str, Any]]:
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
        recorded = _EVENT_HITS.get()
        for hit in hits:
            identity = self._identity(hit)
            hit.ref = self._ref_for(identity)
            state.remember(hit)
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
        return [hit.model_dump(mode="json") for hit in hits]

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
        _, prepared_backend, query, domains, limit, offset = self._prepare_search(
            state, params
        )
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

        return await self._run_provider(
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
        backend_name, _backend, query, domains, limit, offset = self._prepare_search(
            state, params
        )
        await state.policy.record_search()
        normalized_domains = (
            sorted({str(domain).strip() for domain in domains if str(domain).strip()})
            if domains
            else None
        )
        request = {"limit": limit, "offset": offset, "domains": normalized_domains}
        fingerprint = self._fingerprint(
            {
                "backend": backend_name,
                "revision": self.backend_revision,
                "query": query,
                **request,
            }
        )
        key = self._flight_key(f"{backend_name}.search", fingerprint)
        admission = await self._admit_flights(
            state,
            {key: (fingerprint, [0])},
            group_new=False,
        )
        for group in admission.new_groups:

            async def execute(
                group: _FlightGroup = group,
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
                        failure=self._provider_failure(exc),
                        request=request,
                    )
                else:
                    batch = SearchBatch(query=query, hits=hits, request=request)
                return {key: batch}

            self._start_flight_group(state, group, execute)

        batch = SearchBatch.model_validate(
            await self._await_flight(state, admission.waiters[key])
        )
        if batch.failure is not None:
            raise CapabilityProviderError.from_failure(
                batch.failure.model_dump(mode="json"),
                attempts=len(_EVENT_PROVIDER_ATTEMPTS.get() or []),
            )
        return self._record_search_hits(state, list(batch.hits))

    @staticmethod
    def _canonical_url(url: str) -> str:
        """Fold the spellings of a URL that mean the same page.

        Conservative on purpose: lower-case scheme and host, drop the fragment,
        drop known tracking parameters, sort what remains. No redirect
        following, no rel=canonical, no path normalisation -- those need a
        network round trip or a guess, and a wrong merge silently deletes a page
        the program could otherwise have read.
        """
        parts = urlsplit(url.strip())
        query = sorted(
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if key.lower() not in _TRACKING_PARAMS
        )
        encoded = "&".join(f"{key}={value}" for key, value in query)
        return urlunsplit(
            (parts.scheme.lower(), parts.netloc.lower(), parts.path, encoded, "")
        )

    @classmethod
    def _identity(cls, hit: SearchHit) -> str:
        """A stable key for the document behind a hit.

        Two jobs, and the second is the one that is easy to miss. It lets
        duplicate candidates be counted across queries -- but it is also what
        makes a reference reproducible: with a random handle per sighting, two
        replays of the same recorded search produce different refs, and any
        program that sorts or keys on a ref stops being deterministic.

        Backends are never merged. A local docid and a web URL can describe the
        same text, but nothing here can prove it, and a wrong merge is silent.
        """
        if hit.docid:
            return f"{hit.backend}:docid:{hit.docid}"
        if hit.url:
            return f"{hit.backend}:url:{cls._canonical_url(hit.url)}"
        # A backend that returns neither is degenerate, but falling back to a
        # random key would quietly reintroduce the unreproducibility this method
        # exists to remove.
        digest = hashlib.sha256(f"{hit.title}\n{hit.snippet}".encode()).hexdigest()[:32]
        return f"{hit.backend}:text:{digest}"

    @staticmethod
    def _ref_for(identity: str) -> str:
        """An opaque, session-independent handle derived from the identity.

        Opaque because a program must not construct one; derived because the
        same document has to come back as the same ref for deduplication and
        replay to work at all.
        """
        return f"ref_{hashlib.sha256(identity.encode()).hexdigest()[:16]}"

    async def _search_query_many(
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
                    request=request,
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
                    request=request,
                )
                continue
            fingerprint = self._fingerprint(
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
            self._record_deduplicated_request(
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
        elif leaders and backend_name == "local" and isinstance(
            backend, BatchSearchBackend
        ):
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
            gate = asyncio.Semaphore(
                min(max(int(params.get("concurrency", 5)), 1), 20)
            )

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
                            failure=self._provider_failure(exc),
                            request=request,
                        )
                    return SearchBatch(
                        query=queries[index],
                        hits=hits,
                        request=request,
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
                batch.hits = [
                    SearchHit.model_validate(hit)
                    for hit in self._record_search_hits(
                        state,
                        list(batch.hits),
                        query_index=index,
                    )
                ]

        provider_failures = [
            batch.failure.model_dump(mode="json")
            for index, batch in enumerate(finalized)
            if index in fingerprints and batch.failure is not None
        ]
        if provider_failures and len(provider_failures) == len(fingerprints) and all(
            self._is_systemic_search_failure(failure)
            for failure in provider_failures
        ):
            attempts = len(_EVENT_PROVIDER_ATTEMPTS.get() or [])
            raise CapabilityProviderError.from_failures(
                provider_failures,
                attempts=attempts,
            )
        return [batch.model_dump(mode="json") for batch in finalized]

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
            index: self._flight_key(operation, fingerprints[index]) for index in leaders
        }
        admission = await self._admit_flights(
            state,
            {
                key_for_index[index]: (fingerprints[index], [index])
                for index in leaders
            },
            group_new=backend_name == "local" and isinstance(backend, BatchSearchBackend),
        )
        index_for_key = {key: index for index, key in key_for_index.items()}
        gate = asyncio.Semaphore(concurrency)
        for group in admission.new_groups:
            group_indexes = [index_for_key[key] for key in group.keys]
            group_indexes.sort()
            if backend_name == "local" and isinstance(backend, BatchSearchBackend):

                async def execute_local(
                    group: _FlightGroup = group,
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
                    return {
                        key_for_index[index]: row for index, row in rows.items()
                    }

                self._start_flight_group(state, group, execute_local)
                continue

            if len(group_indexes) != 1:
                raise RuntimeError("non-batch search flight contains multiple keys")
            index = group_indexes[0]
            key = key_for_index[index]

            async def execute_one(
                group: _FlightGroup = group,
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
                            failure=self._provider_failure(exc),
                            request=request,
                        )
                    else:
                        row = SearchBatch(
                            query=queries[index],
                            hits=hits,
                            request=request,
                        )
                return {key: row}

            self._start_flight_group(state, group, execute_one)

        returned = await asyncio.gather(
            *(
                self._await_flight(state, admission.waiters[key_for_index[index]])
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
            returned = await self._run_provider(
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
            failure = self._provider_failure(exc)
            return {
                index: SearchBatch(
                    query=queries[index],
                    failure=failure,
                    request=request,
                )
                for index in leaders
            }
        if len(returned) != len(leaders):
            raise RuntimeError("Provider runtime accepted an invalid batch result count.")
        attempts = max(
            (
                record.attempt
                for record in (_EVENT_PROVIDER_ATTEMPTS.get() or [])
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
                request=request,
            )
        if any(row.failure is not None for row in rows.values()):
            for record in reversed(_EVENT_PROVIDER_ATTEMPTS.get() or []):
                if record.operation_id == operation_id and record.status == "success":
                    record.status = "partial"
                    break
        return rows

    @staticmethod
    def _lookup(state: BrokerSession, handle: str) -> SearchHit | None:
        """One handle to the hit behind it, or None if this session never saw it.

        Three tables, one admission rule. The raise below is the enforcement
        point of the capability boundary -- the sandbox has no network, so a
        search is the only way a document can enter reach -- and widening the
        set of accepted *keys* does not widen the set of reachable *documents*.
        A docid the corpus contains but no query in this session returned is
        still refused, which is what keeps a recall metric meaningful.

        The `ref_` fallback is the one spelling repair allowed here, and it is
        allowed because it is not a guess: it is an exact hit on a full key
        after restoring a constant prefix, so it resolves to one document or to
        none. Fuzzy repair is deliberately absent. Programs mistype these
        handles constantly -- a third of the rejected ones are a single
        character from a real ref -- and with a few hundred refs in a 16-hex
        space a nearest-match would nearly always be right. Nearly always right
        about *which document* is the wrong trade: it converts an error the
        program can see and recover from into a silent read of, and citation
        to, a document nobody asked for. The transcription problem is real and
        is fixed by not making programs copy handles, not here.
        """
        return (
            state.references.get(handle)
            or state.by_docid.get(handle)
            or state.by_url.get(handle)
            # A ref with the prefix dropped, which is what printing
            # `ref.split("_")[1]` or reading a truncated column produces.
            or state.references.get(f"ref_{handle}")
        )

    def _resolve_refs(self, state: BrokerSession, refs: list[str]) -> list[SearchHit]:
        # A bare string is iterable, so without this a single handle passed
        # unwrapped is resolved one character at a time and the program is told
        # `Unknown references: r, e, f`, which names nothing it can act on.
        # Accepting it resolves the same document the wrapped form would.
        if isinstance(refs, str):
            refs = [refs]
        resolved = [(handle, self._lookup(state, str(handle))) for handle in refs]
        missing = [handle for handle, hit in resolved if hit is None]
        if missing:
            raise ValueError(
                f"Unknown references: {', '.join(str(handle) for handle in missing[:3])}. "
                "Pass a ref, docid, or URL that a search in this session returned."
            )
        hits = [hit for _, hit in resolved if hit is not None]
        # Every consumer of a handle passes through here -- the four content
        # methods and `citations.resolve` -- so this is the one place that knows
        # which documents a non-search event touched.
        #
        # Recording it is what makes the second half of the retrieval funnel
        # measurable. `search.*` events already carry identities, so a trace
        # could always answer "did the gold document ever surface"; without this
        # it could not answer "was it ever opened", and those two questions have
        # completely different remedies. Reconstructing it afterwards is not
        # possible: refs are opaque in the transcript and the ref->docid table
        # dies with the session.
        #
        # `rank` is the rank of the sighting `remember()` kept, i.e. where the
        # document first appeared, not a property of this call. That is the
        # useful reading -- "the program opened something it had seen at rank 34"
        # -- and it is the only rank a content event has.
        recorded = _EVENT_HITS.get()
        if recorded is not None:
            recorded.extend(
                HitRecord(identity=self._identity(hit), rank=hit.rank, score=hit.score)
                for hit in hits
            )
        return hits

    def _resolve_content_refs(
        self, state: BrokerSession, refs: Any
    ) -> list[SearchHit]:
        if isinstance(refs, str):
            normalized = [refs]
        elif isinstance(refs, list):
            normalized = refs
        else:
            raise ValueError("content refs must be a list or a single ref")
        if len(normalized) > self.max_content_refs_per_request:
            raise ValueError(
                f"content request contains {len(normalized)} refs, exceeding the "
                f"broker maximum of {self.max_content_refs_per_request}"
            )
        return self._resolve_refs(state, normalized)

    async def _session_usage(
        self,
        state: BrokerSession,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """What this session has spent so far.

        Evaluation sessions omit a budget and retain measurement-only behaviour.
        RL sessions additionally receive their remaining hard allowances and a
        typed terminal reason.

        Counted as a capability call like any other. An exception for it would
        make the trace stop being a complete record of what the program asked
        for.
        """
        del params
        usage = state.policy.usage
        return {
            "exec_calls": usage.exec_calls,
            "search_calls": usage.search_calls,
            "content_fetches": usage.content_fetches,
            "llm_calls": usage.llm_calls,
            "pipeline_model_tokens": usage.pipeline_model_tokens,
            "documents_seen": len(state.references),
            "budget_remaining": state.policy.remaining(),
            "terminal_reason": state.policy.terminal_reason,
        }

    async def _resolve_citations(
        self,
        state: BrokerSession,
        params: dict[str, Any],
    ) -> list[dict[str, Any]]:
        if "requests" in params:
            raw_requests = params.get("requests")
            if not isinstance(raw_requests, list):
                raise ValueError("citation requests must be a list")
            requests = raw_requests
        else:
            raw_refs = params.get("refs", [])
            if isinstance(raw_refs, str):
                raw_refs = [raw_refs]
            if not isinstance(raw_refs, list):
                raise ValueError("citation refs must be a list")
            requests = [{"ref": ref} for ref in raw_refs]

        resolved: list[dict[str, Any]] = []
        for request in requests:
            if not isinstance(request, dict):
                raise ValueError("Each citation request must be an object")
            if set(request) - {"ref", "locator"}:
                raise ValueError("Citation requests accept only ref and locator")
            handle = request.get("ref")
            if not isinstance(handle, str) or not handle:
                raise ValueError("Every citation request must contain a non-empty ref")
            hit = self._resolve_refs(state, [handle])[0]
            if "locator" not in request:
                resolved.append(self._citation_wire(hit, hit.snippet, "search_preview"))
                continue
            locator = request["locator"]
            if locator is None:
                raise ValueError(
                    "Citation locator must be omitted for legacy preview citations; "
                    "explicit null is not valid passage evidence"
                )
            record = self._verify_evidence_locator(state, hit.ref, locator)
            citation = self._citation_wire(hit, record.text, record.kind)
            citation["locator"] = locator
            resolved.append(citation)
        return resolved

    @staticmethod
    def _citation_wire(
        hit: SearchHit,
        evidence: str,
        evidence_kind: str,
    ) -> dict[str, Any]:
        return {
            "ref": hit.ref,
            "title": hit.title,
            "url": hit.url,
            "docid": hit.docid,
            "evidence": evidence,
            "evidence_kind": evidence_kind,
            "backend": hit.backend,
        }

    def _verify_evidence_locator(
        self,
        state: BrokerSession,
        ref: str,
        locator: Any,
    ) -> EvidenceRecord:
        locator_id = locator.get("id") if isinstance(locator, dict) else None
        registered: EvidenceRecord | None = None

        def reject(message: str, code: str) -> None:
            self._record_evidence_trace(
                locator_id=locator_id if isinstance(locator_id, str) else None,
                ref=ref,
                action="validate",
                status="error",
                record=registered,
                error_code=code,
            )
            raise ValueError(message)

        if not isinstance(locator, dict) or set(locator) != {"id", "ref", "kind"}:
            reject(
                "Evidence locator must contain exactly id, ref, and kind",
                "invalid_locator",
            )
        if not all(isinstance(locator[key], str) for key in ("id", "ref", "kind")):
            reject("Evidence locator fields must be strings", "invalid_locator")
        if not locator["id"] or len(locator["id"]) > 128:
            reject("Evidence locator id is empty or too long", "invalid_locator")
        registered = state.evidence.get(locator["id"])
        if locator["kind"] != "selected_passage":
            reject("Unsupported evidence locator kind", "invalid_locator_kind")
        if locator["ref"] != ref:
            reject(
                "Evidence locator does not belong to the requested ref",
                "locator_ref_mismatch",
            )
        if registered is None:
            reject("Unknown evidence locator", "unknown_locator")
        assert registered is not None
        if registered.ref != locator["ref"] or registered.kind != locator["kind"]:
            reject("Evidence locator has been altered", "altered_locator")
        self._record_evidence_trace(
            locator_id=locator["id"],
            ref=ref,
            action="validate",
            status="ok",
            record=registered,
        )
        return registered

    @staticmethod
    def _record_evidence_trace(
        *,
        locator_id: str | None,
        ref: str,
        action: str,
        status: str,
        record: EvidenceRecord | None,
        error_code: str | None = None,
    ) -> None:
        traced = _EVENT_EVIDENCE.get()
        if traced is None:
            return
        traced.append(
            EvidenceTraceRecord(
                locator_id=locator_id[:128] if locator_id else None,
                ref=ref,
                action=action,
                status=status,
                coordinates=dict(record.coordinates) if record else {},
                document_fingerprint=record.document_fingerprint if record else None,
                passage_fingerprint=record.passage_fingerprint if record else None,
                error_code=error_code,
            )
        )

    def _register_evidence(
        self,
        state: BrokerSession,
        *,
        ref: str,
        text: str,
        document_text: str,
        coordinates: dict[str, Any],
    ) -> tuple[dict[str, str] | None, dict[str, Any] | None]:
        if not text or len(text) > self.max_evidence_chars:
            return None, None
        document_fingerprint = hashlib.sha256(document_text.encode("utf-8")).hexdigest()
        passage_fingerprint = hashlib.sha256(text.encode("utf-8")).hexdigest()
        material = json.dumps(
            {
                "ref": ref,
                "kind": "selected_passage",
                "coordinates": coordinates,
                "document_fingerprint": document_fingerprint,
                "passage_fingerprint": passage_fingerprint,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        locator_id = "evidence_" + hashlib.sha256(
            f"{state.session.token}\0{material}".encode()
        ).hexdigest()[:24]
        record = EvidenceRecord(
            ref=ref,
            kind="selected_passage",
            text=text,
            coordinates=dict(coordinates),
            document_fingerprint=document_fingerprint,
            passage_fingerprint=passage_fingerprint,
        )
        existing = state.evidence.get(locator_id)
        locator = {"id": locator_id, "ref": ref, "kind": "selected_passage"}
        if existing is not None:
            if existing != record:
                self._record_evidence_trace(
                    locator_id=locator_id,
                    ref=ref,
                    action="issue",
                    status="error",
                    record=existing,
                    error_code="evidence_locator_collision",
                )
                raise RuntimeError("Evidence locator collision detected")
            self._record_evidence_trace(
                locator_id=locator_id,
                ref=ref,
                action="issue",
                status="ok",
                record=existing,
            )
            return locator, None

        passage_bytes = len(text.encode("utf-8"))
        if (
            len(state.evidence) >= self.max_evidence_records
            or state.evidence_passage_bytes + passage_bytes
            > self.max_evidence_passage_bytes
        ):
            self._record_evidence_trace(
                locator_id=locator_id,
                ref=ref,
                action="issue",
                status="error",
                record=record,
                error_code="evidence_capacity_exhausted",
            )
            return None, {
                "code": "evidence_capacity_exhausted",
                "message": "The session evidence registry is full.",
                "retryable": False,
            }

        state.evidence[locator_id] = record
        state.evidence_passage_bytes += passage_bytes
        state.policy.set_evidence_usage(
            records=len(state.evidence),
            passage_bytes=state.evidence_passage_bytes,
        )
        self._record_evidence_trace(
            locator_id=locator_id,
            ref=ref,
            action="issue",
            status="ok",
            record=record,
        )
        return locator, None

    async def _content_get_many(
        self,
        state: BrokerSession,
        params: dict[str, Any],
    ) -> list[dict[str, Any]]:
        hits = self._resolve_content_refs(state, params.get("refs", []))
        return await self._fetch_content(state, hits, query=None)

    async def _content_snippets(
        self,
        state: BrokerSession,
        params: dict[str, Any],
    ) -> list[dict[str, Any]]:
        hits = self._resolve_content_refs(state, params.get("refs", []))
        query = str(params.get("query", ""))
        rows = await self._fetch_content(state, hits, query=query)
        per_page_chars = max(int(params.get("max_tokens_per_page", 1000)), 1) * 4
        total_chars = max(int(params.get("max_tokens", 4000)), 1) * 4
        used = 0
        for row in rows:
            document_text = row["text"]
            text, metadata = self._select_passage(document_text, query, per_page_chars)
            row["metadata"] = {**row.get("metadata", {}), **metadata}
            remaining = max(total_chars - used, 0)
            if len(text) > remaining:
                text = text[:remaining]
                row["metadata"]["truncated_by_total_budget"] = True
                if not text:
                    row["metadata"]["omitted_by_total_budget"] = True
            start = int(row["metadata"].get("passage_start", 0))
            row["metadata"]["passage_end"] = start + len(text)
            row["text"] = text
            used += len(text)
            locator, locator_error = self._register_evidence(
                state,
                ref=str(row.get("ref") or ""),
                text=text,
                document_text=self._normalize_text(document_text),
                coordinates={
                    "type": "characters",
                    "basis": "normalized_text",
                    "start": start,
                    "end": start + len(text),
                },
            )
            if locator is not None:
                row["locator"] = locator
            if locator_error is not None:
                row["locator_error"] = locator_error
        # Positional alignment is part of the batch contract. A page which lost
        # its passage to the total budget remains as an explicit empty row.
        return rows

    # Documents in a research corpus are mostly longer than any budget that can
    # be handed to a control model -- median ~9k characters, p90 ~62k -- so
    # `get_many` (whole document) and `snippets` (one broker-chosen window) both
    # answer "show me this page" and neither answers "show me the part of this
    # page I can name". Without a third option the passage a program sees is
    # decided by a scoring function it cannot inspect, and if the answer is not
    # in that window nothing downstream can recover it.
    #
    # `read` and `grep` are that third option, and they are deliberately the
    # same pair the function-calling profiles already expose, with the same
    # 1-indexed line contract: a line number from `grep` is an `offset` for
    # `read`, no character arithmetic anywhere. Both work on the text a backend
    # returned, so neither knows which backend it is on.

    @staticmethod
    def _document_lines(row: dict[str, Any]) -> list[str]:
        return str(row.get("text") or "").splitlines()

    async def _content_read(
        self,
        state: BrokerSession,
        params: dict[str, Any],
    ) -> list[dict[str, Any]]:
        hits = self._resolve_content_refs(state, params.get("refs", []))
        rows = await self._fetch_content(state, hits, query=None)
        # 1-indexed, and an offset below 1 is clamped rather than refused: a
        # program computing `match.line - 5` near the top of a document is
        # asking for the beginning, not making an error.
        offset = max(int(params.get("offset", 1)), 1)
        limit = min(max(int(params.get("limit", 200)), 1), 5_000)
        # A line is not a fixed amount of text. In the local corpus a line is a
        # sentence; in a scraped web page it is often a whole section, so the
        # same `limit` spans two orders of magnitude between backends. This is
        # a ceiling on the response, not a budget the program is meant to
        # manage -- generous enough that ordinary reading never meets it.
        max_chars = min(max(int(params.get("max_chars", 100_000)), 1), 400_000)
        windows: list[dict[str, Any]] = []
        for row in rows:
            document_text = str(row.get("text") or "")
            lines = self._document_lines(row)
            total = len(lines)
            window = lines[offset - 1 : offset - 1 + limit]
            # Trim by whole lines, so `end_line` keeps meaning what it says and
            # a follow-up read resumes on a real boundary.
            clipped = False
            while window and len("\n".join(window)) > max_chars and len(window) > 1:
                window.pop()
                clipped = True
            text = "\n".join(window)
            partial_line = len(window) == 1 and len(text) > max_chars
            if partial_line:
                text = text[:max_chars]
                clipped = True
            end = offset - 1 + len(window)
            metadata = {
                **row.get("metadata", {}),
                "start_line": offset if window else 0,
                "end_line": end,
                "total_lines": total,
                # None at end of document, so `while next_offset:` is a correct
                # scroll loop.
                "next_offset": end + 1 if end < total else None,
            }
            if clipped:
                metadata["truncated_by_max_chars"] = True
            if partial_line:
                # The public read coordinate is line-based, so a single line
                # cannot be resumed mid-line. Report the partial window
                # explicitly and bind its locator with character coordinates
                # instead of claiming the prefix represents the whole line.
                metadata["truncated_mid_line"] = True
                metadata["partial_line_remaining_chars"] = len(window[0]) - len(text)
            result = {**row, "text": text, "metadata": metadata}
            coordinates = (
                {
                    "type": "line_characters",
                    "line": metadata["start_line"],
                    "start_character": 0,
                    "end_character": len(text),
                }
                if partial_line
                else {
                    "type": "lines",
                    "start_line": metadata["start_line"],
                    "end_line": metadata["end_line"],
                }
            )
            locator, locator_error = self._register_evidence(
                state,
                ref=str(row.get("ref") or ""),
                text=text,
                document_text=document_text,
                coordinates=coordinates,
            )
            if locator is not None:
                result["locator"] = locator
            if locator_error is not None:
                result["locator_error"] = locator_error
            windows.append(result)
        return windows

    @staticmethod
    def _compile_pattern(pattern: str) -> re.Pattern[str]:
        """Case-insensitive, and a malformed regex degrades to a literal search.

        A program that meant to search for ``C++ (programming)`` should get its
        matches rather than a traceback about an unbalanced parenthesis.
        """
        try:
            return re.compile(pattern, flags=re.IGNORECASE)
        except re.error:
            return re.compile(re.escape(pattern), flags=re.IGNORECASE)

    async def _content_grep(
        self,
        state: BrokerSession,
        params: dict[str, Any],
    ) -> list[dict[str, Any]]:
        matches, _ = await self._content_grep_rows(state, params, report=False)
        return matches

    async def _content_grep_report(
        self,
        state: BrokerSession,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        matches, failures = await self._content_grep_rows(state, params, report=True)
        raw_refs = params.get("refs", [])
        input_count = 1 if isinstance(raw_refs, str) else len(raw_refs)
        return {
            "matches": matches,
            "failures": failures,
            "input_count": input_count,
        }

    async def _content_grep_rows(
        self,
        state: BrokerSession,
        params: dict[str, Any],
        *,
        report: bool,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        pattern = str(params.get("pattern", ""))
        if not pattern:
            raise ValueError("pattern must not be empty")
        hits = self._resolve_content_refs(state, params.get("refs", []))
        rows = await self._fetch_content(
            state,
            hits,
            query=None,
            raise_on_all_failures=not report,
        )
        regex = self._compile_pattern(pattern)
        context = min(max(int(params.get("context", 0)), 0), 20)
        # Bounded per document rather than in total: an unbounded grep over 50
        # candidates is how a program fills its own output budget with one call,
        # and a global cap would let the first document starve the other 49.
        max_per_ref = min(max(int(params.get("max_matches_per_ref", 20)), 1), 200)
        matches: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        for input_index, row in enumerate(rows):
            failure = row.get("failure")
            if failure is None and row.get("metadata", {}).get("fetch_error"):
                failure = {
                    "code": "provider_rejected",
                    "message": "Provider rejected one document.",
                    "retryable": False,
                    "attempts": 1,
                }
            if failure is not None:
                failures.append(
                    {
                        "input_index": input_index,
                        "ref": row.get("ref", ""),
                        "failure": failure,
                    }
                )
                continue
            lines = self._document_lines(row)
            metadata = row.get("metadata", {})
            found = 0
            for index, line in enumerate(lines):
                if found >= max_per_ref:
                    break
                if not regex.search(line):
                    continue
                found += 1
                before = lines[max(0, index - context) : index] if context else []
                after = lines[index + 1 : index + 1 + context] if context else []
                match = {
                    "ref": row.get("ref", ""),
                    "docid": metadata.get("docid"),
                    "url": row.get("url"),
                    "title": row.get("title", ""),
                    "line": index + 1,
                    "text": line,
                    "before": before,
                    "after": after,
                }
                if report:
                    match["input_index"] = input_index
                evidence = "\n".join([*before, line, *after])
                locator, locator_error = self._register_evidence(
                    state,
                    ref=str(row.get("ref") or ""),
                    text=evidence,
                    document_text=str(row.get("text") or ""),
                    coordinates={
                        "type": "lines",
                        "start_line": index + 1 - len(before),
                        "end_line": index + 1 + len(after),
                        "match_line": index + 1,
                    },
                )
                if locator is not None:
                    match["locator"] = locator
                if locator_error is not None:
                    match["locator_error"] = locator_error
                matches.append(match)
        return matches, failures

    @staticmethod
    def _normalize_text(text: str) -> str:
        normalized = (text or "").replace("\r\n", "\n").replace("\r", "\n")
        return re.sub(r"\n{3,}", "\n\n", normalized).strip()

    @staticmethod
    def _word_tokens(text: str) -> list[str]:
        return re.findall(r"\w+", (text or "").lower())

    @classmethod
    def _score_passage(cls, passage: str, query: str) -> float:
        normalized_passage = passage.strip()
        normalized_query = query.strip()
        if not normalized_passage or not normalized_query:
            return 0.0
        query_tokens = set(cls._word_tokens(normalized_query))
        passage_tokens = set(cls._word_tokens(normalized_passage))
        overlap_recall = (
            sum(1 for token in query_tokens if token in passage_tokens)
            / max(1, len(query_tokens))
        )
        char_similarity = SequenceMatcher(
            None,
            normalized_query.lower(),
            normalized_passage.lower(),
        ).ratio()
        return overlap_recall * 0.85 + char_similarity * 0.15

    @classmethod
    def _select_passage(
        cls,
        text: str,
        query: str,
        max_chars: int,
    ) -> tuple[str, dict[str, Any]]:
        normalized = cls._normalize_text(text)
        if not normalized:
            return "", {"passage_score": 0.0}
        paragraphs = [
            part.strip() for part in re.split(r"\n\s*\n+", normalized) if part.strip()
        ]
        if not paragraphs or not query.strip():
            return normalized[:max_chars], {
                "passage_index": 0,
                "passage_score": 0.0,
                "passage_start": 0,
                "passage_end": min(len(normalized), max_chars),
            }

        positions: list[tuple[int, int]] = []
        scores: list[float] = []
        start = 0
        for paragraph in paragraphs:
            positions.append((start, start + len(paragraph)))
            scores.append(cls._score_passage(paragraph, query))
            start += len(paragraph) + 2
        best_index = max(range(len(paragraphs)), key=scores.__getitem__)
        best_start, best_end = positions[best_index]
        remaining = max(0, max_chars - len(paragraphs[best_index]))
        window_start = max(0, best_start - remaining // 2)
        window_end = best_end + remaining // 2
        selected_indexes = [
            index
            for index, (paragraph_start, paragraph_end) in enumerate(positions)
            if index == best_index
            or (paragraph_start >= window_start and paragraph_end <= window_end)
        ]
        snippet = cls._normalize_text(
            "\n\n".join(paragraphs[index] for index in selected_indexes)
        )[:max_chars]
        selected_start = positions[selected_indexes[0]][0]
        selected_end = min(
            positions[selected_indexes[-1]][1],
            selected_start + len(snippet),
        )
        return snippet, {
            "passage_index": best_index,
            "passage_score": scores[best_index],
            "passage_start": selected_start,
            "passage_end": selected_end,
        }

    async def _fetch_content(
        self,
        state: BrokerSession,
        hits: list[SearchHit],
        *,
        query: str | None,
        raise_on_all_failures: bool = False,
    ) -> list[dict[str, Any]]:
        """Text for every requested hit, in the order requested.

        Three properties the callers above depend on. One row per hit, so a
        program can pair results with what it asked for. Caller order, so
        pairing is positional and not a join. And a document already read in
        this session is served from the cache: `grep` and `read` exist to be
        used repeatedly over one pool, and refetching it per stage is
        affordable against a local index but is three times the bill and the
        latency against a paid scrape API.
        """
        keys: list[str] = []
        leaders: dict[str, tuple[int, SearchHit]] = {}
        for index, hit in enumerate(hits):
            state.policy.require_backend(hit.backend)
            fingerprint = self._fingerprint(
                {
                    "backend": hit.backend,
                    "revision": self.backend_revision,
                    "ref": hit.ref,
                }
            )
            keys.append(fingerprint)
            leader = leaders.get(fingerprint)
            if leader is None:
                leaders[fingerprint] = (index, hit)
                continue
            self._record_deduplicated_request(
                request_index=index,
                leader_index=leader[0],
                request_fingerprint=fingerprint,
            )

        duplicate_count = len(hits) - len(leaders)
        if duplicate_count:
            state.policy.record_deduplicated(duplicate_count)
        misses = {
            key: value
            for key, value in leaders.items()
            if value[1].ref not in state.content_cache
        }
        await state.policy.record_content_fetches(len(hits), 0)
        # The logical-usage reservation above may yield while another caller's
        # flight completes. Its transport leader writes the cache before
        # removing that flight, so refreshing here closes the only window in
        # which this call could admit a second leader for an already-cached
        # document.
        misses = {
            key: value
            for key, value in misses.items()
            if value[1].ref not in state.content_cache
        }

        async def fetch_one(
            key: str,
            input_index: int,
            hit: SearchHit,
            *,
            operation_id: str | None = None,
            track_execution: bool = True,
        ) -> tuple[str, dict[str, Any]]:
            backend = self.backends.get(hit.backend)
            if backend is None:
                return key, self._content_failure_row(
                    hit,
                    {
                        "code": "provider_not_configured",
                        "message": f"Backend '{hit.backend}' is not configured.",
                        "retryable": False,
                        "attempts": 0,
                    },
                )
            operation = "local.document" if hit.backend == "local" else "web.scrape"

            async def request() -> ContentSnippet:
                fetch = getattr(backend, "fetch", None)
                if callable(fetch):
                    result = await fetch(hit, query=query)
                else:
                    # Compatibility for third-party/test backends written to
                    # the 0.2 batch protocol. Bundled adapters never use this.
                    content = getattr(backend, "content", None)
                    if not callable(content):
                        raise ValueError("backend has no atomic content fetch operation")
                    returned = await content([hit], query=query)
                    if not isinstance(returned, list) or len(returned) != 1:
                        raise ValueError("backend returned an invalid content row count")
                    result = returned[0]
                row = ContentSnippet.model_validate(result)
                if row.ref != hit.ref:
                    raise ValueError("backend changed the requested content ref")
                return row

            validate_fetch = getattr(backend, "preflight_fetch", None)

            def preflight() -> None:
                if callable(validate_fetch):
                    validate_fetch(hit)
                # This counter represents admitted unique provider leaders,
                # not logical input rows. Keeping it in the runtime preflight
                # means an invalid handle or missing credential is rejected
                # before either this usage or a governor token is consumed.
                state.policy.record_content_backend_fetches(1)

            try:
                snippet = await self._run_provider(
                    state,
                    backend=backend,
                    operation=operation,
                    request_indexes=[input_index],
                    request_value={
                        "backend": hit.backend,
                        "revision": self.backend_revision,
                        "ref": hit.ref,
                    },
                    request=request,
                    preflight=preflight,
                    operation_id=operation_id,
                    track_execution=track_execution,
                )
            except ProviderRequestError as exc:
                return key, self._content_failure_row(
                    hit,
                    self._provider_failure(exc),
                )

            row = snippet.model_dump(mode="json")
            legacy_error = row.get("metadata", {}).get("fetch_error")
            if row.get("failure") is None and legacy_error:
                attempts = max(
                    (
                        record.attempt
                        for record in (_EVENT_PROVIDER_ATTEMPTS.get() or [])
                        if input_index in record.request_indexes
                        and record.operation == operation
                    ),
                    default=1,
                )
                return key, self._content_failure_row(
                    hit,
                    {
                        "code": "provider_rejected",
                        "message": "Provider rejected one document.",
                        "retryable": False,
                        "attempts": attempts,
                    },
                )
            if row.get("date") is None and hit.date is not None:
                row["date"] = hit.date
            return key, row

        if self.inflight_coalescing and misses:
            flight_key_for_fingerprint = {
                fingerprint: self._flight_key(
                    "local.document" if hit.backend == "local" else "web.scrape",
                    fingerprint,
                )
                for fingerprint, (_index, hit) in misses.items()
            }
            admission = await self._admit_flights(
                state,
                {
                    flight_key_for_fingerprint[fingerprint]: (
                        fingerprint,
                        [input_index],
                    )
                    for fingerprint, (input_index, _hit) in misses.items()
                },
                group_new=False,
            )
            fingerprint_for_flight_key = {
                flight_key: fingerprint
                for fingerprint, flight_key in flight_key_for_fingerprint.items()
            }
            for group in admission.new_groups:
                if len(group.keys) != 1:
                    raise RuntimeError("content flight contains multiple keys")
                flight_key = next(iter(group.keys))
                fingerprint = fingerprint_for_flight_key[flight_key]
                input_index, hit = misses[fingerprint]
                cached = state.content_cache.get(hit.ref)
                if cached is not None:

                    async def execute_cached_content(
                        flight_key: str = flight_key,
                        cached: dict[str, Any] = cached,
                    ) -> dict[str, dict[str, Any]]:
                        # Cache and flight admission are separate structures.
                        # A previous leader may populate the cache while this
                        # call waits for the flight lock; publish that row
                        # through the newly admitted future without starting a
                        # second provider operation.
                        return {flight_key: copy.deepcopy(cached)}

                    self._start_flight_group(state, group, execute_cached_content)
                    continue

                async def execute_content(
                    group: _FlightGroup = group,
                    flight_key: str = flight_key,
                    fingerprint: str = fingerprint,
                    input_index: int = input_index,
                    hit: SearchHit = hit,
                ) -> dict[str, dict[str, Any]]:
                    _key, row = await fetch_one(
                        fingerprint,
                        input_index,
                        hit,
                        operation_id=group.operation_id,
                        track_execution=False,
                    )
                    # Publish successful content to the session before the
                    # flight runner removes its active key or resolves waiter
                    # futures. A caller queued behind flight cleanup therefore
                    # observes either the active flight or the cache, never a
                    # gap between the two.
                    state.cache_content(row, self.session_content_cache_bytes)
                    return {flight_key: row}

                self._start_flight_group(state, group, execute_content)

            returned_rows = await asyncio.gather(
                *(
                    self._await_flight(
                        state,
                        admission.waiters[flight_key_for_fingerprint[fingerprint]],
                    )
                    for fingerprint in misses
                )
            )
            fetched = dict(zip(misses, returned_rows, strict=True))
        else:
            returned = await asyncio.gather(
                *(
                    fetch_one(key, input_index, hit)
                    for key, (input_index, hit) in misses.items()
                )
            )
            fetched = dict(returned)
        for row in fetched.values():
            state.cache_content(row, self.session_content_cache_bytes)

        rows: list[dict[str, Any]] = []
        for key, hit in zip(keys, hits, strict=True):
            source = state.content_cache.get(hit.ref) or fetched.get(key)
            if source is None:
                source = self._content_failure_row(
                    hit,
                    {
                        "code": "provider_invalid_response",
                        "message": "Provider returned no result for this document.",
                        "retryable": False,
                        "attempts": 0,
                    },
                )
            row = copy.deepcopy(source)
            if row.get("date") is None and hit.date is not None:
                row["date"] = hit.date
            rows.append(row)

        failures = [row["failure"] for row in rows if row.get("failure") is not None]
        all_failed = bool(rows) and len(failures) == len(rows)
        all_systemic = all_failed and all(
            self._is_systemic_content_failure(failure) for failure in failures
        )
        if all_systemic or (raise_on_all_failures and all_failed):
            raise CapabilityProviderError.from_failures(
                failures,
                attempts=len(_EVENT_PROVIDER_ATTEMPTS.get() or []),
            )
        return rows

    @staticmethod
    def _content_failure_row(
        hit: SearchHit,
        failure: dict[str, Any],
    ) -> dict[str, Any]:
        return ContentSnippet(
            ref=hit.ref,
            text="",
            url=hit.url,
            title=hit.title,
            date=hit.date,
            failure=failure,
            metadata={"backend": hit.backend},
        ).model_dump(mode="json")

    async def _chat(
        self,
        prompt: str,
        *,
        system: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_object: bool = False,
    ) -> tuple[str, int]:
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        options: dict[str, Any] = {}
        if temperature is not None:
            options["temperature"] = temperature
        if max_tokens is not None:
            options["max_completion_tokens"] = max_tokens
        if json_object:
            options["response_format"] = {"type": "json_object"}
        async with self.capacity_gate.slot():
            response = await self.model_client.chat.completions.create(
                model=self.extraction_model,
                messages=messages,
                **options,
            )
        usage = getattr(response, "usage", None)
        tokens = int(getattr(usage, "total_tokens", 0) or 0)
        return response.choices[0].message.content or "", tokens

    def _require_model(self) -> None:
        if self.model_client is None or not self.extraction_model:
            raise RuntimeError("LLM access is not configured")

    @staticmethod
    def _clamp_temperature(value: Any) -> float:
        return min(max(float(value), 0.0), 2.0)

    @staticmethod
    def _clamp_max_tokens(value: Any) -> int | None:
        if value is None:
            return None
        return min(max(int(value), 1), 32_000)

    async def _complete(self, state: BrokerSession, params: dict[str, Any]) -> str:
        self._require_model()
        prompt = str(params.get("prompt", "")).strip()
        if not prompt:
            raise ValueError("prompt must not be empty")
        max_tokens = await state.policy.reserve_llm(
            1,
            max_tokens=self._clamp_max_tokens(params.get("max_tokens")),
        )
        system = params.get("system")
        answer, tokens = await self._chat(
            prompt,
            system=str(system) if system else None,
            temperature=self._clamp_temperature(params.get("temperature", 0.2)),
            max_tokens=max_tokens,
        )
        await state.policy.record_pipeline_model_tokens(tokens)
        _EVENT_MODEL_TOKENS.set(_EVENT_MODEL_TOKENS.get() + tokens)
        return answer

    async def _complete_many(self, state: BrokerSession, params: dict[str, Any]) -> list[str]:
        self._require_model()
        prompts = [str(prompt) for prompt in params.get("prompts", [])]
        if not prompts:
            return []
        if any(not prompt.strip() for prompt in prompts):
            raise ValueError("prompts must not contain empty strings")
        # The whole fan-out is counted before it runs, so a batch that dies
        # partway is still reported at the size it was dispatched at rather than
        # at however far it got.
        max_tokens = await state.policy.reserve_llm(
            len(prompts),
            max_tokens=self._clamp_max_tokens(params.get("max_tokens")),
        )
        system = params.get("system")
        temperature = self._clamp_temperature(params.get("temperature", 0.2))
        concurrency = min(max(int(params.get("concurrency", 4)), 1), 12)
        gate = asyncio.Semaphore(concurrency)

        async def one(prompt: str) -> tuple[str, int]:
            async with gate:
                return await self._chat(
                    prompt,
                    system=str(system) if system else None,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )

        results = await asyncio.gather(*(one(prompt) for prompt in prompts))
        total_tokens = sum(tokens for _, tokens in results)
        await state.policy.record_pipeline_model_tokens(total_tokens)
        _EVENT_MODEL_TOKENS.set(_EVENT_MODEL_TOKENS.get() + total_tokens)
        return [answer for answer, _ in results]

    _SCHEMA_KEYWORDS = frozenset(
        {
            "$schema",
            "type",
            "properties",
            "required",
            "additionalProperties",
            "items",
            "enum",
            "description",
        }
    )
    _SCHEMA_TYPES = frozenset(
        {"object", "array", "string", "integer", "number", "boolean", "null"}
    )
    _REPAIRABLE_EXTRACTION_ERRORS = frozenset(
        {"empty_output", "invalid_json", "non_object", "schema_mismatch"}
    )

    @staticmethod
    def _json_payload(value: Any, label: str) -> str:
        try:
            return json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
        except (TypeError, ValueError):
            raise ValueError(f"{label} must contain only JSON-serializable values") from None

    def _validate_schema_subset(self, schema: Any) -> Draft202012Validator:
        if not isinstance(schema, dict):
            raise ValueError("schema must be a JSON object")
        if schema.get("type") != "object":
            raise ValueError("schema root must declare type 'object'")

        def visit(node: Any, *, depth: int, path: str) -> None:
            if not isinstance(node, dict):
                raise ValueError(f"schema node at {path} must be an object")
            if depth > self.max_extract_schema_depth:
                raise ValueError(
                    f"schema nesting exceeds maximum depth {self.max_extract_schema_depth}"
                )
            unknown = sorted(set(node) - self._SCHEMA_KEYWORDS)
            if unknown:
                raise ValueError(
                    f"schema keyword '{unknown[0]}' at {path} is not supported"
                )
            declared_type = node.get("type")
            base_type: str | None = None
            if declared_type is not None:
                if isinstance(declared_type, str):
                    if declared_type not in self._SCHEMA_TYPES:
                        raise ValueError(
                            f"schema type '{declared_type}' at {path} is not supported"
                        )
                    base_type = declared_type
                elif isinstance(declared_type, list):
                    if (
                        len(declared_type) != 2
                        or any(
                            not isinstance(item, str) or item not in self._SCHEMA_TYPES
                            for item in declared_type
                        )
                        or len(set(declared_type)) != 2
                        or "null" not in declared_type
                    ):
                        raise ValueError(
                            f"schema type list at {path} must contain one type plus null"
                        )
                    base_type = next(item for item in declared_type if item != "null")
                else:
                    raise ValueError(f"schema type at {path} must be a string or list")

            if "$schema" in node:
                dialect = node["$schema"]
                if dialect not in {
                    "https://json-schema.org/draft/2020-12/schema",
                    "https://json-schema.org/draft/2020-12/schema#",
                }:
                    raise ValueError("only JSON Schema Draft 2020-12 is supported")
            if "description" in node and not isinstance(node["description"], str):
                raise ValueError(f"schema description at {path} must be a string")
            if "enum" in node and (
                not isinstance(node["enum"], list) or not node["enum"]
            ):
                raise ValueError(f"schema enum at {path} must be a non-empty list")

            object_keys = {"properties", "required", "additionalProperties"} & set(node)
            if object_keys and base_type not in {None, "object"}:
                raise ValueError(f"object keywords at {path} require type 'object'")
            properties = node.get("properties")
            if properties is not None:
                if not isinstance(properties, dict):
                    raise ValueError(f"schema properties at {path} must be an object")
                for name, child in properties.items():
                    if not isinstance(name, str):
                        raise ValueError(f"schema property names at {path} must be strings")
                    visit(child, depth=depth + 1, path=f"{path}.properties.{name}")
            if "required" in node:
                required = node["required"]
                if not isinstance(required, list) or any(
                    not isinstance(item, str) for item in required
                ):
                    raise ValueError(f"schema required at {path} must be a list of strings")
            if "additionalProperties" in node and not isinstance(
                node["additionalProperties"], bool
            ):
                raise ValueError(
                    f"schema additionalProperties at {path} must be a boolean"
                )

            if "items" in node:
                if base_type not in {None, "array"}:
                    raise ValueError(f"schema items at {path} requires type 'array'")
                visit(node["items"], depth=depth + 1, path=f"{path}.items")

        visit(schema, depth=1, path="$")
        try:
            Draft202012Validator.check_schema(schema)
        except Exception as exc:
            raise ValueError(f"invalid JSON Schema: {self._trace_error_message(exc)}") from None
        return Draft202012Validator(schema)

    def _prepare_extraction(
        self,
        params: dict[str, Any],
    ) -> tuple[list[Any], list[str], str, str, Draft202012Validator, int, int]:
        items = params.get("items", [])
        if not isinstance(items, list):
            raise ValueError("items must be a list")
        if len(items) > self.max_extract_items:
            raise ValueError(
                f"extract_many contains {len(items)} items, exceeding the broker maximum "
                f"of {self.max_extract_items}"
            )
        instruction = params.get("instruction", "")
        if not isinstance(instruction, str):
            raise ValueError("instruction must be a string")
        instruction_bytes = len(instruction.encode("utf-8"))
        if instruction_bytes > self.max_extract_instruction_bytes:
            raise ValueError(
                f"instruction is {instruction_bytes} bytes, exceeding the broker maximum "
                f"of {self.max_extract_instruction_bytes}"
            )

        schema = params.get("schema", {})
        schema_json = self._json_payload(schema, "schema")
        schema_bytes = len(schema_json.encode("utf-8"))
        if schema_bytes > self.max_extract_schema_bytes:
            raise ValueError(
                f"schema is {schema_bytes} bytes, exceeding the broker maximum "
                f"of {self.max_extract_schema_bytes}"
            )
        validator = self._validate_schema_subset(schema)

        item_json: list[str] = []
        total_item_bytes = 0
        for index, item in enumerate(items):
            encoded = self._json_payload(item, f"item at index {index}")
            size = len(encoded.encode("utf-8"))
            if size > self.max_extract_item_bytes:
                raise ValueError(
                    f"item at index {index} is {size} bytes, exceeding the broker maximum "
                    f"of {self.max_extract_item_bytes}"
                )
            total_item_bytes += size
            item_json.append(encoded)
        if total_item_bytes > self.max_extract_total_item_bytes:
            raise ValueError(
                f"items total {total_item_bytes} bytes, exceeding the broker maximum "
                f"of {self.max_extract_total_item_bytes}"
            )

        repair_attempts = params.get("repair_attempts", 0)
        if isinstance(repair_attempts, bool) or not isinstance(repair_attempts, int):
            raise ValueError("repair_attempts must be 0 or 1")
        if repair_attempts not in {0, 1}:
            raise ValueError("repair_attempts must be 0 or 1")
        if repair_attempts > self.max_extract_repair_attempts:
            raise ValueError(
                f"repair_attempts exceeds the broker maximum of "
                f"{self.max_extract_repair_attempts}"
            )
        try:
            concurrency = int(params.get("concurrency", 4))
        except (TypeError, ValueError):
            raise ValueError("concurrency must be an integer") from None
        concurrency = min(max(concurrency, 1), 12)
        return (
            items,
            item_json,
            instruction,
            schema_json,
            validator,
            repair_attempts,
            concurrency,
        )

    async def _model_output(
        self,
        prompt: str,
        *,
        max_tokens: int | None,
        gate: asyncio.Semaphore,
    ) -> _ModelOutput:
        started = time.monotonic()
        try:
            async with gate:
                content, tokens = await self._chat(
                    prompt,
                    max_tokens=max_tokens,
                    json_object=True,
                )
        except Exception:
            return _ModelOutput(
                content=None,
                tokens=0,
                duration_seconds=time.monotonic() - started,
                provider_failed=True,
            )
        return _ModelOutput(
            content=content,
            tokens=tokens,
            duration_seconds=time.monotonic() - started,
        )

    @staticmethod
    def _strict_json_object(
        content: str | None,
    ) -> tuple[dict[str, Any] | None, _ExtractionError | None]:
        if content is None or not content.strip():
            return None, _ExtractionError("empty_output", "Model returned an empty output")

        def reject_constant(_: str) -> Any:
            raise ValueError("non-finite number")

        def finite_float(value: str) -> float:
            parsed = float(value)
            if not math.isfinite(parsed):
                raise ValueError("non-finite number")
            return parsed

        def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate object key")
                result[key] = value
            return result

        try:
            parsed = json.loads(
                content,
                parse_constant=reject_constant,
                parse_float=finite_float,
                object_pairs_hook=unique_object,
            )
        except (json.JSONDecodeError, ValueError):
            return None, _ExtractionError(
                "invalid_json",
                "Model returned invalid strict JSON",
            )
        if not isinstance(parsed, dict):
            return None, _ExtractionError(
                "non_object",
                "Model output must be one JSON object",
            )
        return parsed, None

    @classmethod
    def _checked_extraction(
        cls,
        content: str | None,
        validator: Draft202012Validator,
    ) -> tuple[dict[str, Any] | None, _ExtractionError | None]:
        data, error = cls._strict_json_object(content)
        if error is not None:
            return None, error
        assert data is not None
        failures = sorted(validator.iter_errors(data), key=lambda item: list(item.path))
        if failures:
            first = failures[0]
            location = "$" + "".join(
                f"[{part}]" if isinstance(part, int) else f".{part}" for part in first.path
            )
            return None, _ExtractionError(
                "schema_mismatch",
                f"Model output does not match schema at {location} ({first.validator})",
            )
        return data, None

    async def _record_model_tokens(
        self,
        state: BrokerSession,
        outputs: list[_ModelOutput],
    ) -> None:
        total_tokens = sum(output.tokens for output in outputs)
        await state.policy.record_pipeline_model_tokens(total_tokens)
        _EVENT_MODEL_TOKENS.set(_EVENT_MODEL_TOKENS.get() + total_tokens)

    @staticmethod
    def _append_model_attempts(
        indexes: list[int],
        phase: str,
        outputs: list[_ModelOutput],
        errors: list[_ExtractionError | None],
    ) -> None:
        recorded = _EVENT_MODEL_ATTEMPTS.get()
        if recorded is None:
            return
        for index, output, error in zip(indexes, outputs, errors, strict=True):
            code = "provider_error" if output.provider_failed else error.code if error else None
            recorded.append(
                ModelAttemptRecord(
                    index=index,
                    phase=phase,
                    status="error" if code else "ok",
                    duration_seconds=output.duration_seconds,
                    model_tokens=output.tokens,
                    error_code=code,
                )
            )

    async def _extract_many(
        self,
        state: BrokerSession,
        params: dict[str, Any],
    ) -> list[dict[str, Any]]:
        (
            items,
            item_json,
            instruction,
            schema_json,
            validator,
            repair_attempts,
            concurrency,
        ) = self._prepare_extraction(params)
        self._require_model()
        if not items:
            return []
        requested_max_tokens = self._clamp_max_tokens(params.get("max_tokens"))
        max_tokens = await state.policy.reserve_llm(
            len(items),
            max_tokens=requested_max_tokens,
        )
        gate = asyncio.Semaphore(concurrency)

        def initial_prompt(index: int) -> str:
            prompt = (
                f"{instruction}\n\nJSON schema:\n{schema_json}\n\n"
                f"Input:\n{item_json[index]}\n\n"
                "Return only one JSON object."
            )
            return prompt

        initial_outputs = await asyncio.gather(
            *(
                self._model_output(initial_prompt(index), max_tokens=max_tokens, gate=gate)
                for index in range(len(items))
            )
        )
        await self._record_model_tokens(state, initial_outputs)

        checked: list[tuple[dict[str, Any] | None, _ExtractionError | None]] = []
        for output in initial_outputs:
            if output.provider_failed:
                checked.append(
                    (
                        None,
                        _ExtractionError(
                            "provider_error",
                            "Extraction provider request failed",
                            retryable=True,
                        ),
                    )
                )
            else:
                checked.append(self._checked_extraction(output.content, validator))
        self._append_model_attempts(
            list(range(len(items))),
            "initial",
            initial_outputs,
            [error for _, error in checked],
        )

        if all(output.provider_failed for output in initial_outputs):
            raise ExtractionInfrastructureError()

        results = [
            {
                "index": index,
                "data": data,
                "error": error.wire() if error else None,
                "attempts": 1,
            }
            for index, (data, error) in enumerate(checked)
        ]
        repair_indexes = [
            index
            for index, (_, error) in enumerate(checked)
            if repair_attempts
            and error is not None
            and error.code in self._REPAIRABLE_EXTRACTION_ERRORS
        ]
        if not repair_indexes:
            return results

        # Reserve the complete, index-ordered repair set before dispatching any
        # second attempt. A tight budget cannot make completion order decide
        # which malformed rows get repaired.
        repair_max_tokens = await state.policy.reserve_llm(
            len(repair_indexes),
            max_tokens=requested_max_tokens,
        )

        def repair_prompt(index: int) -> str:
            assert initial_outputs[index].content is not None
            error = checked[index][1]
            assert error is not None
            return (
                f"{instruction}\n\nJSON schema:\n{schema_json}\n\n"
                f"Input:\n{item_json[index]}\n\n"
                f"Previous invalid output:\n{initial_outputs[index].content}\n\n"
                f"Validation error:\n{error.code}: {error.message}\n\n"
                "Repair the output. Return only one JSON object matching the schema."
            )

        repair_outputs = await asyncio.gather(
            *(
                self._model_output(
                    repair_prompt(index),
                    max_tokens=repair_max_tokens,
                    gate=gate,
                )
                for index in repair_indexes
            )
        )
        await self._record_model_tokens(state, repair_outputs)
        repair_checked: list[tuple[dict[str, Any] | None, _ExtractionError | None]] = []
        for output in repair_outputs:
            if output.provider_failed:
                repair_checked.append(
                    (
                        None,
                        _ExtractionError(
                            "provider_error",
                            "Extraction provider request failed",
                            retryable=True,
                        ),
                    )
                )
            else:
                repair_checked.append(self._checked_extraction(output.content, validator))
        self._append_model_attempts(
            repair_indexes,
            "repair",
            repair_outputs,
            [error for _, error in repair_checked],
        )
        for index, (data, error) in zip(repair_indexes, repair_checked, strict=True):
            results[index] = {
                "index": index,
                "data": data,
                "error": error.wire() if error else None,
                "attempts": 2,
            }
        return results
