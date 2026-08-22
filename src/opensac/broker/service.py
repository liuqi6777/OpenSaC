from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any

from openai import AsyncOpenAI

from opensac.backends.rerank.base import ClosablePassageReranker, PassageReranker
from opensac.backends.search.base import ClosableSearchBackend, SearchBackend
from opensac.broker.call_context import CallContext, call_scope, trace_error_message
from opensac.broker.content import ContentCapabilities
from opensac.broker.llm import LLMCapabilities
from opensac.broker.policy import CapabilityPolicy, MechanismDisabled
from opensac.broker.provider_execution import ProviderExecutor
from opensac.broker.search import SearchCapabilities
from opensac.broker.session import BrokerSession
from opensac.metrics import CapacityGate
from opensac.models import CAPABILITY_METHODS, CapabilityEvent, Session
from opensac.provider import ProviderPolicy, ProviderRuntime

CapabilityHandler = Callable[[BrokerSession, dict[str, Any]], Awaitable[Any]]


class BrokerService:
    """Compose and trace the broker's capability families."""

    def __init__(
        self,
        backends: dict[str, SearchBackend],
        *,
        model_client: AsyncOpenAI | None = None,
        extraction_model: str = "",
        passage_reranker: PassageReranker | None = None,
        passage_chunk_chars: int = 2_000,
        passage_chunk_overlap_chars: int = 200,
        passage_prefilter_limit: int = 100,
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
        max_content_sources_per_request: int = 256,
        content_url_admission: str = "searched_or_public_web",
        content_batch_deadline_seconds: float = 60.0,
        inflight_coalescing: bool = False,
        max_inflight_keys: int = 256,
        max_waiters_per_flight: int = 64,
        provider_result_cache_ttl_seconds: float = 0.0,
        provider_result_cache_max_bytes: int = 128_000_000,
        provider_runtime: ProviderRuntime | None = None,
        backend_revision: str = "",
    ) -> None:
        self.backends = backends
        self.passage_reranker = passage_reranker
        self.sessions: dict[str, BrokerSession] = {}
        self.capacity_gate = CapacityGate(max_concurrency)
        self.max_context_payload_bytes = max_context_payload_bytes

        retrieval_limits = {
            "max_search_queries_per_request": max_search_queries_per_request,
            "max_search_query_chars": max_search_query_chars,
            "max_search_top_k": max_search_top_k,
        }
        validated_retrieval = self._positive_limits(retrieval_limits)
        self.max_search_queries_per_request = validated_retrieval["max_search_queries_per_request"]
        self.max_search_query_chars = validated_retrieval["max_search_query_chars"]
        self.max_search_top_k = validated_retrieval["max_search_top_k"]

        passage_limits = self._positive_limits(
            {
                "passage_chunk_chars": passage_chunk_chars,
                "passage_prefilter_limit": passage_prefilter_limit,
            }
        )
        self.passage_chunk_chars = passage_limits["passage_chunk_chars"]
        self.passage_prefilter_limit = passage_limits["passage_prefilter_limit"]
        if self.passage_prefilter_limit > 100:
            raise ValueError("passage_prefilter_limit cannot exceed 100")
        if int(passage_chunk_overlap_chars) < 0:
            raise ValueError("passage_chunk_overlap_chars cannot be negative")
        if int(passage_chunk_overlap_chars) >= self.passage_chunk_chars:
            raise ValueError("passage_chunk_overlap_chars must be smaller than chunk size")
        self.passage_chunk_overlap_chars = int(passage_chunk_overlap_chars)

        component_limits = self._positive_limits(
            {
                "max_extract_items": max_extract_items,
                "max_extract_instruction_bytes": max_extract_instruction_bytes,
                "max_extract_schema_bytes": max_extract_schema_bytes,
                "max_extract_item_bytes": max_extract_item_bytes,
                "max_extract_total_item_bytes": max_extract_total_item_bytes,
                "max_extract_schema_depth": max_extract_schema_depth,
                "max_content_sources_per_request": max_content_sources_per_request,
                "max_inflight_keys": max_inflight_keys,
                "max_waiters_per_flight": max_waiters_per_flight,
            }
        )
        if int(max_extract_repair_attempts) not in {0, 1}:
            raise ValueError("max_extract_repair_attempts must be 0 or 1")

        self.provider_runtime = provider_runtime or ProviderRuntime(
            {
                "local.search": ProviderPolicy(concurrency=max_concurrency),
                "web.search": ProviderPolicy(concurrency=max_concurrency),
                "local.document": ProviderPolicy(concurrency=6),
                "web.scrape": ProviderPolicy(concurrency=6),
                "web.rerank": ProviderPolicy(concurrency=2),
            }
        )
        self.providers = ProviderExecutor(
            self.sessions,
            self.provider_runtime,
            inflight_coalescing=bool(inflight_coalescing),
            max_inflight_keys=component_limits["max_inflight_keys"],
            max_waiters_per_flight=component_limits["max_waiters_per_flight"],
            result_cache_ttl_seconds=provider_result_cache_ttl_seconds,
            result_cache_max_bytes=provider_result_cache_max_bytes,
        )
        self.search = SearchCapabilities(
            backends,
            self.providers,
            backend_revision=backend_revision,
            max_queries_per_request=self.max_search_queries_per_request,
            max_query_chars=self.max_search_query_chars,
            max_top_k=self.max_search_top_k,
        )
        self.content = ContentCapabilities(
            backends,
            self.providers,
            passage_reranker=passage_reranker,
            passage_chunk_chars=self.passage_chunk_chars,
            passage_chunk_overlap_chars=self.passage_chunk_overlap_chars,
            passage_prefilter_limit=self.passage_prefilter_limit,
            max_query_chars=self.max_search_query_chars,
            max_sources_per_request=component_limits["max_content_sources_per_request"],
            session_content_cache_bytes=int(session_content_cache_bytes),
            content_url_admission=content_url_admission,
            content_batch_deadline_seconds=content_batch_deadline_seconds,
            backend_revision=backend_revision,
        )
        self.llm = LLMCapabilities(
            model_client,
            extraction_model,
            self.capacity_gate,
            max_extract_items=component_limits["max_extract_items"],
            max_instruction_bytes=component_limits["max_extract_instruction_bytes"],
            max_schema_bytes=component_limits["max_extract_schema_bytes"],
            max_item_bytes=component_limits["max_extract_item_bytes"],
            max_total_item_bytes=component_limits["max_extract_total_item_bytes"],
            max_schema_depth=component_limits["max_extract_schema_depth"],
            max_repair_attempts=int(max_extract_repair_attempts),
        )
        self._handlers: dict[str, CapabilityHandler] = {
            "search.query": self.search.query,
            "search.query_many": self.search.query_many,
            "content.get_many": self.content.get_many,
            "content.passages": self.content.passages,
            "content.read": self.content.read,
            "content.grep_report": self.content.grep_report,
            "session.usage": self._session_usage,
            "llm.complete": self.llm.complete,
            "llm.complete_many": self.llm.complete_many,
            "llm.extract_many": self.llm.extract_many,
        }
        assert set(self._handlers) == set(CAPABILITY_METHODS), (
            "The handler table and models.CAPABILITY_METHODS have diverged."
        )

    @property
    def execution_tasks(self) -> dict[tuple[str, str], set[asyncio.Task[Any]]]:
        """Expose active calls to the API lifecycle without duplicating their state."""

        return self.providers.execution_tasks

    @staticmethod
    def _positive_limits(values: dict[str, int]) -> dict[str, int]:
        validated: dict[str, int] = {}
        for name, value in values.items():
            if int(value) < 1:
                raise ValueError(f"{name} must be at least 1")
            validated[name] = int(value)
        return validated

    async def aclose(self) -> None:
        """Close provider work and backend-owned connection pools."""

        await self.providers.aclose()
        closable: list[ClosableSearchBackend] = []
        seen: set[int] = set()
        for backend in self.backends.values():
            if id(backend) in seen or not isinstance(backend, ClosableSearchBackend):
                continue
            seen.add(id(backend))
            closable.append(backend)
        await asyncio.gather(*(backend.aclose() for backend in closable))
        if isinstance(self.passage_reranker, ClosablePassageReranker):
            await self.passage_reranker.aclose()

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

    async def cancel_execution(
        self,
        token: str,
        execution_id: str,
        reason: str = "provider_cancelled",
    ) -> int:
        return await self.providers.cancel_execution(token, execution_id, reason)

    async def cancel_session(self, token: str) -> int:
        return await self.providers.cancel_session(token)

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
            return await self._call_traced(token, method, params)
        task = asyncio.create_task(
            self._call_traced(token, method, params, execution_id=execution_id)
        )
        self.providers.track_execution_task(token, execution_id, task)
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
        handler = self._handlers.get(method)
        if handler is None:
            raise ValueError(f"Unsupported capability: {method}")

        sequence = state.next_trace_sequence()
        started = time.monotonic()
        with call_scope(token, execution_id) as context:
            try:
                blocked = state.mechanisms.blocked_reason(method) or state.mechanisms.fanout_reason(
                    method, params
                )
                if blocked:
                    raise MechanismDisabled(blocked)
                result = await handler(state, params)
            except asyncio.CancelledError as exc:
                event = self._event(
                    state,
                    context,
                    sequence=sequence,
                    method=method,
                    params=params,
                    started=started,
                    status="cancelled",
                    exc=exc,
                    error="Capability execution was cancelled.",
                )
                self._finish_trace(state, execution_id, context, event)
                raise
            except Exception as exc:
                event = self._event(
                    state,
                    context,
                    sequence=sequence,
                    method=method,
                    params=params,
                    started=started,
                    status="error",
                    exc=exc,
                    error=trace_error_message(exc),
                )
                self._finish_trace(state, execution_id, context, event)
                raise
            else:
                payload, truncated = self._context_payload(state, result)
                event = self._event(
                    state,
                    context,
                    sequence=sequence,
                    method=method,
                    params=params,
                    started=started,
                    status="ok",
                    result=result,
                    result_payload=payload,
                    result_payload_truncated=truncated,
                )
                self._finish_trace(state, execution_id, context, event)
                return result

    def _event(
        self,
        state: BrokerSession,
        context: CallContext,
        *,
        sequence: int,
        method: str,
        params: dict[str, Any],
        started: float,
        status: str,
        result: Any = None,
        exc: BaseException | None = None,
        error: str | None = None,
        result_payload: Any = None,
        result_payload_truncated: bool = False,
    ) -> CapabilityEvent:
        return CapabilityEvent(
            sequence=sequence,
            method=method,
            status=status,
            duration_seconds=time.monotonic() - started,
            queries=self._trace_queries(method, params),
            input_count=self._trace_input_count(method, params),
            result_count=self._trace_result_count(method, result) if status == "ok" else 0,
            hits=list(context.hits),
            model_tokens=context.model_tokens,
            model_attempts=list(context.model_attempts),
            passage_records=list(context.passage_records),
            provider_attempts=list(context.provider_attempts),
            deduplicated_requests=list(context.deduplicated_requests),
            coalesced_requests=list(context.coalesced_requests),
            provider_cache_hits=context.provider_cache_hits,
            provider_cache_misses=context.provider_cache_misses,
            error_type=type(exc).__name__ if exc is not None else None,
            error=error,
            result_payload=result_payload,
            result_payload_truncated=result_payload_truncated,
        )

    @staticmethod
    def _finish_trace(
        state: BrokerSession,
        execution_id: str | None,
        context: CallContext,
        event: CapabilityEvent,
    ) -> None:
        context.provider_trace.bind(event)
        if execution_id:
            state.traces.setdefault(execution_id, []).append(event)

    def _context_payload(self, state: BrokerSession, result: Any) -> tuple[Any, bool]:
        if state.mechanisms.context_decoupling:
            return None, False
        encoded = json.dumps(result, ensure_ascii=False, default=str)
        if len(encoded.encode("utf-8")) <= self.max_context_payload_bytes:
            return result, False
        return encoded[: self.max_context_payload_bytes], True

    def take_trace(self, token: str, execution_id: str | None) -> list[CapabilityEvent]:
        if not execution_id:
            return []
        state = self.sessions.get(token)
        if state is None:
            return []
        return sorted(state.traces.pop(execution_id, []), key=lambda event: event.sequence)

    def _trace_queries(self, method: str, params: dict[str, Any]) -> list[str]:
        if method == "content.passages":
            query = str(params.get("query", ""))
            return [query[: self.max_search_query_chars]] if query else []
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
        if method.startswith("content."):
            sources = params.get("sources", [])
            return (
                1 if isinstance(sources, str) else len(sources) if isinstance(sources, list) else 0
            )
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
        if method == "content.passages" and isinstance(result, dict):
            return len(result.get("passages", []))
        return 1 if result is not None else 0

    @staticmethod
    async def _session_usage(
        state: BrokerSession,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        del params
        usage = state.policy.usage
        return {
            "exec_calls": usage.exec_calls,
            "search_calls": usage.search_calls,
            "content_fetches": usage.content_fetches,
            "direct_url_attempts": usage.direct_url_attempts,
            "direct_url_successes": usage.direct_url_successes,
            "llm_calls": usage.llm_calls,
            "pipeline_model_tokens": usage.pipeline_model_tokens,
            "documents_seen": len(state.documents_by_id),
            "budget_remaining": state.policy.remaining(),
            "terminal_reason": state.policy.terminal_reason,
        }
