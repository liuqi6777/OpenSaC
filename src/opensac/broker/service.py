from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol

from opensac.backends.document import ClosableDocumentBackend, DocumentBackend
from opensac.backends.llm import ClosableLLMBackend, LLMBackend
from opensac.backends.rerank import ClosableTextReranker, LexicalReranker, TextReranker
from opensac.backends.search.base import ClosableSearchBackend, SearchBackend
from opensac.broker.call_context import CallContext, call_scope, trace_error_message
from opensac.broker.capabilities.catalog import CapabilityBuildContext, CapabilityCatalog
from opensac.broker.config import BrokerConfig
from opensac.broker.policy import CapabilityPolicy, MechanismDisabled
from opensac.broker.providers import BackendBinding, ProviderExecutionConfig, ProviderExecutor
from opensac.broker.registry import (
    CapabilityRegistry,
    CapabilityRequest,
    CapabilitySpec,
)
from opensac.broker.session import BrokerSession
from opensac.models import (
    CAPABILITY_CONTRACT,
    CAPABILITY_METHODS,
    Mechanisms,
    Session,
)
from opensac.provider import ProviderPolicy, ProviderRuntime
from opensac.sandbox import SANDBOX_CONTRACT
from opensac.tracing import CapabilityEvent

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RetrievalRoute:
    search: SearchBackend
    document: DocumentBackend
    revision: str = ""


class CapabilityObserver(Protocol):
    def capability_started(
        self,
        execution_id: str,
        sequence: int,
        method: str,
        params: dict[str, Any],
    ) -> None: ...

    def capability_completed(
        self,
        execution_id: str,
        sequence: int,
        event: CapabilityEvent,
        result: Any,
    ) -> None: ...


class BrokerService:
    """Compose and trace the broker's capability families."""

    def __init__(
        self,
        routes: dict[str, RetrievalRoute],
        *,
        config: BrokerConfig | None = None,
        llm_backend: LLMBackend | None = None,
        reranker: TextReranker | None = None,
        max_concurrency: int = 12,
        provider_execution_config: ProviderExecutionConfig | None = None,
        search_runtime: ProviderRuntime | None = None,
        document_runtime: ProviderRuntime | None = None,
        rerank_runtime: ProviderRuntime | None = None,
        llm_runtime: ProviderRuntime | None = None,
        capability_observer: CapabilityObserver | None = None,
        capability_catalog: CapabilityCatalog | None = None,
        enabled_capabilities: Iterable[str] | None = None,
    ) -> None:
        if not routes:
            raise ValueError("at least one retrieval route must be configured")
        self.config = config or BrokerConfig()
        self.search_backends = {name: route.search for name, route in routes.items()}
        self.document_backends = {name: route.document for name, route in routes.items()}
        self.llm_backend = llm_backend
        self.reranker = reranker if reranker is not None else LexicalReranker()
        self.sessions: dict[str, BrokerSession] = {}
        self.capability_observer = capability_observer

        self.search_runtime = search_runtime or ProviderRuntime(
            ProviderPolicy(concurrency=max_concurrency)
        )
        self.document_runtime = document_runtime or ProviderRuntime(ProviderPolicy(concurrency=6))
        self.rerank_runtime = rerank_runtime or ProviderRuntime(ProviderPolicy(concurrency=2))
        if llm_backend is None and llm_runtime is not None:
            raise ValueError("llm_runtime requires a configured LLM backend")
        self.llm_runtime: ProviderRuntime | None = None
        if llm_backend is not None:
            self.llm_runtime = llm_runtime or ProviderRuntime(
                ProviderPolicy(concurrency=max_concurrency)
            )
        self.service_runtimes = {
            "search": self.search_runtime,
            "document": self.document_runtime,
            "rerank": self.rerank_runtime,
        }
        if self.llm_runtime is not None:
            self.service_runtimes["llm"] = self.llm_runtime
        self._validate_document_backends()
        self.providers = ProviderExecutor(
            self.sessions,
            config=provider_execution_config or ProviderExecutionConfig(),
        )
        self.search_bindings = {
            name: BackendBinding(
                backend=route.search,
                runtime=self.search_runtime,
                component="search",
                route=name,
                revision=route.revision,
            )
            for name, route in routes.items()
        }
        self.document_bindings = {
            name: BackendBinding(
                backend=route.document,
                runtime=self.document_runtime,
                component="document",
                route=name,
                revision=route.revision,
                resource_failures=True,
            )
            for name, route in routes.items()
        }
        self.rerank_binding = BackendBinding(
            backend=self.reranker,
            runtime=self.rerank_runtime,
            component="rerank",
        )
        self.llm_binding = (
            BackendBinding(
                backend=llm_backend,
                runtime=self.llm_runtime,
                component="llm",
            )
            if llm_backend is not None and self.llm_runtime is not None
            else None
        )
        catalog = capability_catalog or CapabilityCatalog.builtin()
        modules = catalog.assemble(
            CapabilityBuildContext(
                providers=self.providers,
                search_bindings=self.search_bindings,
                document_bindings=self.document_bindings,
                rerank_binding=self.rerank_binding,
                llm_binding=self.llm_binding,
                config=self.config,
                default_provider_concurrency=max_concurrency,
                session_manifest=self._capability_manifest_for_state,
            ),
            enabled=enabled_capabilities,
        )
        modules_by_name = {module.name: module for module in modules}
        self.search = modules_by_name.get("search")
        self.content = modules_by_name.get("content")
        self.llm = modules_by_name.get("llm")
        self.registry = CapabilityRegistry(modules)
        unknown_methods = set(self.registry.methods) - set(CAPABILITY_METHODS)
        if unknown_methods:
            raise RuntimeError(
                "Capability registry contains methods outside the core contract: "
                f"{sorted(unknown_methods)}"
            )
        required_session_methods = {"session.capabilities"}
        missing_session_methods = required_session_methods - set(self.registry.methods)
        if missing_session_methods:
            raise RuntimeError(
                "Capability registry is missing required session methods: "
                f"{sorted(missing_session_methods)}"
            )

    @property
    def execution_tasks(self) -> dict[tuple[str, str], set[asyncio.Task[Any]]]:
        """Expose active calls to the API lifecycle without duplicating their state."""

        return self.providers.execution_tasks

    def _validate_document_backends(self) -> None:
        for route, backend in self.document_backends.items():
            source_kind = getattr(backend, "source_kind", None)
            if source_kind not in {"opaque", "public_url"}:
                raise ValueError(
                    f"document backend {route!r} declares invalid source kind {source_kind!r}"
                )
            if not callable(getattr(backend, "fetch_candidates", None)):
                raise ValueError(f"document backend {route!r} must declare fetch_candidates")

    async def aclose(self) -> None:
        """Close provider work and backend-owned connection pools."""

        await self.providers.aclose()
        closable: list[
            ClosableSearchBackend
            | ClosableDocumentBackend
            | ClosableTextReranker
            | ClosableLLMBackend
        ] = []
        seen: set[int] = set()
        all_backends = (
            *self.search_backends.values(),
            *self.document_backends.values(),
            self.reranker,
            self.llm_backend,
        )
        for backend in all_backends:
            if (
                backend is None
                or id(backend) in seen
                or not isinstance(
                    backend,
                    (
                        ClosableSearchBackend,
                        ClosableDocumentBackend,
                        ClosableTextReranker,
                        ClosableLLMBackend,
                    ),
                )
            ):
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

    def capability_manifest(
        self,
        *,
        backend_name: str,
        mechanisms: Mechanisms,
    ) -> dict[str, Any]:
        if backend_name not in self.search_backends:
            raise ValueError(f"Backend '{backend_name}' is not configured")
        return {
            "contracts": {
                "sandbox": SANDBOX_CONTRACT,
                "capability": CAPABILITY_CONTRACT,
            },
            **self.registry.manifest(backend_name=backend_name),
            "mechanisms": mechanisms.model_dump(mode="json"),
        }

    def available_methods(self, mechanisms: Mechanisms) -> tuple[str, ...]:
        return tuple(
            method
            for method in self.registry.available_methods
            if mechanisms.blocked_reason(method) is None
        )

    def provider_service_snapshot(self) -> dict[str, Any]:
        """Expose live capacity state for each configured backend role."""

        snapshot: dict[str, Any] = {
            "search": {
                route: binding.runtime.snapshot(self.providers.provider_identity(binding.backend))
                for route, binding in self.search_bindings.items()
            },
            "document": {
                route: binding.runtime.snapshot(self.providers.provider_identity(binding.backend))
                for route, binding in self.document_bindings.items()
            },
        }
        snapshot["rerank"] = self.rerank_binding.runtime.snapshot(
            self.providers.provider_identity(self.rerank_binding.backend)
        )
        if self.llm_binding is not None:
            snapshot["llm"] = self.llm_binding.runtime.snapshot(
                self.providers.provider_identity(self.llm_binding.backend)
            )
        return snapshot

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
        spec = self.registry.get(method)
        if spec is None:
            raise ValueError(f"Unsupported capability: {method}")

        sequence = state.next_trace_sequence()
        started = time.monotonic()
        self._observe_capability_started(execution_id, sequence, method, params)
        capability_family = method.partition(".")[0]
        request: CapabilityRequest | None = None
        with call_scope(
            token,
            execution_id,
            capability_family=capability_family,
        ) as context:
            try:
                blocked = state.mechanisms.blocked_reason(method)
                if blocked:
                    raise MechanismDisabled(blocked)
                request = spec.parse(params)
                result = await spec.handler(state, request)
            except asyncio.CancelledError as exc:
                event = self._event(
                    context,
                    spec=spec,
                    request=request,
                    sequence=sequence,
                    started=started,
                    status="cancelled",
                    exc=exc,
                    error="Capability execution was cancelled.",
                )
                self._finish_trace(state, execution_id, context, event)
                self._observe_capability_completed(execution_id, sequence, event, None)
                raise
            except Exception as exc:
                event = self._event(
                    context,
                    spec=spec,
                    request=request,
                    sequence=sequence,
                    started=started,
                    status="error",
                    exc=exc,
                    error=trace_error_message(exc),
                )
                self._finish_trace(state, execution_id, context, event)
                self._observe_capability_completed(execution_id, sequence, event, None)
                raise
            else:
                payload, truncated = self._context_payload(state, result)
                event = self._event(
                    context,
                    spec=spec,
                    request=request,
                    sequence=sequence,
                    started=started,
                    status="ok",
                    result=result,
                    result_payload=payload,
                    result_payload_truncated=truncated,
                )
                self._finish_trace(state, execution_id, context, event)
                self._observe_capability_completed(execution_id, sequence, event, result)
                return result

    def _observe_capability_started(
        self,
        execution_id: str | None,
        sequence: int,
        method: str,
        params: dict[str, Any],
    ) -> None:
        if execution_id is None or self.capability_observer is None:
            return
        try:
            self.capability_observer.capability_started(
                execution_id,
                sequence,
                method,
                params,
            )
        except Exception:
            logger.exception("capability_observer_start_failed")

    def _observe_capability_completed(
        self,
        execution_id: str | None,
        sequence: int,
        event: CapabilityEvent,
        result: Any,
    ) -> None:
        if execution_id is None or self.capability_observer is None:
            return
        try:
            self.capability_observer.capability_completed(
                execution_id,
                sequence,
                event,
                result,
            )
        except Exception:
            logger.exception("capability_observer_completion_failed")

    def _event(
        self,
        context: CallContext,
        *,
        spec: CapabilitySpec,
        request: CapabilityRequest | None,
        sequence: int,
        started: float,
        status: str,
        result: Any = None,
        exc: BaseException | None = None,
        error: str | None = None,
        result_payload: Any = None,
        result_payload_truncated: bool = False,
    ) -> CapabilityEvent:
        trace = spec.trace
        return CapabilityEvent(
            sequence=sequence,
            method=spec.method,
            status=status,
            duration_seconds=time.monotonic() - started,
            queries=trace.queries(request) if request is not None else [],
            input_count=trace.input_count(request) if request is not None else 0,
            result_count=trace.result_count(result) if status == "ok" else 0,
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
        max_bytes = self.config.max_context_payload_bytes
        if len(encoded.encode("utf-8")) <= max_bytes:
            return result, False
        return encoded[:max_bytes], True

    def take_trace(self, token: str, execution_id: str | None) -> list[CapabilityEvent]:
        if not execution_id:
            return []
        state = self.sessions.get(token)
        if state is None:
            return []
        return sorted(state.traces.pop(execution_id, []), key=lambda event: event.sequence)

    def _capability_manifest_for_state(self, state: BrokerSession) -> dict[str, Any]:
        backend_names = sorted(state.policy.allowed_backends & set(self.search_backends))
        if len(backend_names) != 1:
            raise RuntimeError("A session must have exactly one configured search backend")
        return self.capability_manifest(
            backend_name=backend_names[0],
            mechanisms=state.mechanisms,
        )
