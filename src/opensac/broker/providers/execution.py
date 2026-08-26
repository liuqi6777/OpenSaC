from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from opensac.broker.call_context import current_call
from opensac.broker.session import BrokerSession
from opensac.provider import (
    ProviderAttempt,
    ProviderRequestError,
    ProviderRuntime,
    ProviderWait,
    contextualize_provider_error,
    infer_failure_scope,
)
from opensac.tracing import (
    CoalescedRequestRecord,
    DeduplicatedRequestRecord,
    ProviderAttemptRecord,
)

from .cache import ProviderResultCache
from .config import ProviderExecutionConfig
from .flights import ProviderFlightCoordinator
from .serialization import canonical_json_bytes


class CapabilityProviderError(RuntimeError):
    """A provider failure promoted to a top-level capability RPC error."""

    def __init__(
        self,
        *,
        code: str,
        message: str,
        retryable: bool,
        attempts: int,
        provider_status: int | None = None,
        retry_after_seconds: float | None = None,
        provider: str | None = None,
        component: str | None = None,
        scope: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.attempts = max(int(attempts), 0)
        self.provider_status = provider_status
        self.retry_after_seconds = retry_after_seconds
        self.provider = provider
        self.component = component
        self.scope = scope

    @classmethod
    def from_failure(
        cls,
        failure: dict[str, Any],
        *,
        attempts: int | None = None,
    ) -> CapabilityProviderError:
        return cls(
            code=str(failure.get("code") or "provider_invalid_response"),
            message=str(failure.get("message") or "Provider request failed."),
            retryable=bool(failure.get("retryable")),
            attempts=(int(failure.get("attempts") or 0) if attempts is None else attempts),
            provider_status=failure.get("provider_status"),
            retry_after_seconds=failure.get("retry_after_seconds"),
            provider=failure.get("provider"),
            component=failure.get("component"),
            scope=failure.get("scope"),
        )


class ProviderExecutor:
    """Coordinate provider execution shared by reusable broker services."""

    def __init__(
        self,
        sessions: dict[str, BrokerSession],
        *,
        config: ProviderExecutionConfig,
    ) -> None:
        self.sessions = sessions
        self.flights = ProviderFlightCoordinator(config)
        self.execution_tasks: dict[tuple[str, str], set[asyncio.Task[Any]]] = {}
        self.result_cache = ProviderResultCache(
            ttl_seconds=config.result_cache_ttl_seconds,
            max_bytes=config.result_cache_max_bytes,
        )

    async def aclose(self) -> None:
        await asyncio.gather(*(self.flights.cancel_all(state) for state in self.sessions.values()))
        pending = {
            task for tasks in self.execution_tasks.values() for task in tasks if not task.done()
        }
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self.execution_tasks.clear()
        await self.result_cache.clear()

    def track_execution_task(
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
            await self.flights.detach_execution(state, execution_id)
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
            cancelled_flights = await self.flights.cancel_all(state)
        keys = [key for key in self.execution_tasks if key[0] == token]
        return cancelled_flights + sum(
            await asyncio.gather(
                *(self.cancel_execution(*key) for key in keys),
            )
        )

    @staticmethod
    def fingerprint(value: Any) -> str:
        """Versioned digest of a normalized, secret-free logical value."""

        encoded = canonical_json_bytes(value)
        return "sha256:v1:" + hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def record_deduplicated_request(
        *,
        request_index: int,
        leader_index: int,
        request_fingerprint: str,
    ) -> None:
        context = current_call()
        if context is not None:
            context.deduplicated_requests.append(
                DeduplicatedRequestRecord(
                    request_index=request_index,
                    leader_index=leader_index,
                    request_fingerprint=request_fingerprint,
                )
            )

    @staticmethod
    def provider_failure(error: ProviderRequestError) -> dict[str, Any]:
        failure: dict[str, Any] = {
            "code": error.code,
            "message": error.message,
            "retryable": error.retryable,
            "attempts": error.attempts,
            "provider": error.provider,
            "component": error.component,
            "scope": error.scope,
        }
        if error.provider_status is not None:
            failure["provider_status"] = error.provider_status
        if error.retry_after_seconds is not None:
            failure["retry_after_seconds"] = error.retry_after_seconds
        return failure

    @staticmethod
    def provider_name(backend: Any) -> str:
        """Return a stable, secret-free provider label for public failures."""

        configured = str(getattr(backend, "provider_name", "") or "").strip()
        if configured:
            return configured
        return str(getattr(backend, "name", "") or "unknown")

    @staticmethod
    def provider_identity(backend: Any) -> str:
        return str(getattr(backend, "provider_identity", "") or f"backend:{id(backend)}")

    def contextualize_failure(
        self,
        failure: dict[str, Any],
        *,
        backend: Any,
        component: str,
        resource_failures: bool = False,
    ) -> dict[str, Any]:
        """Fill stable diagnostic fields on broker-created failure rows."""

        contextualized = dict(failure)
        if not contextualized.get("provider"):
            contextualized["provider"] = self.provider_name(backend)
        if not contextualized.get("component"):
            contextualized["component"] = component
        if not contextualized.get("scope"):
            contextualized["scope"] = infer_failure_scope(
                contextualized.get("code") or "provider_invalid_response",
                provider_status=contextualized.get("provider_status"),
                resource_failures=resource_failures,
            )
        return contextualized

    async def run(
        self,
        state: BrokerSession,
        *,
        runtime: ProviderRuntime,
        backend: Any,
        component: str,
        namespace: str,
        resource_failures: bool = False,
        request_indexes: list[int],
        request_value: Any,
        request: Callable[[], Awaitable[Any]],
        preflight: Callable[[], None] | None = None,
        request_id: str | None = None,
        track_execution: bool = True,
    ) -> Any:
        """Execute, account and trace one backend request."""

        request_id = request_id or f"req_{uuid.uuid4().hex}"
        request_fingerprint = self.fingerprint(request_value)
        context = current_call()
        if context is None:
            raise RuntimeError("provider services require a capability call context")
        records = context.provider_attempts
        trace_buffer = context.provider_trace
        provider_name = self.provider_name(backend)
        trace_provider = str(getattr(backend, "name", "") or provider_name)

        def observe(attempt: ProviderAttempt) -> None:
            state.policy.record_provider_attempt(
                capability=context.capability_family,
                attempt=attempt.attempt,
            )
            record = ProviderAttemptRecord(
                request_id=request_id,
                attempt_id=f"{request_id}:{attempt.attempt}",
                provider=trace_provider,
                component=component,
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
            trace_buffer.append(record)

        def observe_wait(wait: ProviderWait) -> None:
            state.policy.record_provider_timing(
                phase=wait.phase,
                duration_seconds=wait.duration_seconds,
            )

        provider_identity = self.provider_identity(backend)
        cache = self.result_cache
        cache_enabled = cache.enabled and bool(getattr(backend, "result_cacheable", False))
        cache_key = cache.key(namespace, provider_identity, request_fingerprint)
        if cache_enabled:
            hit, cached = await cache.get(cache_key)
            if hit:
                state.policy.record_provider_cache(hit=True)
                context.provider_cache_hits += 1
                return cached
            state.policy.record_provider_cache(hit=False)
            context.provider_cache_misses += 1

        async def execute_provider() -> Any:
            task = asyncio.create_task(
                runtime.run(
                    request,
                    provider_identity=provider_identity,
                    request_indexes=request_indexes,
                    preflight=preflight,
                    observer=observe,
                    wait_observer=observe_wait,
                )
            )
            session_token = context.session_token
            if session_token and track_execution:
                self.track_execution_task(
                    session_token,
                    context.execution_id,
                    task,
                )
            try:
                return await task
            except ProviderRequestError as exc:
                raise contextualize_provider_error(
                    exc,
                    provider=provider_name,
                    component=component,
                    resource_failures=resource_failures,
                ) from exc

        if cache_enabled:
            async with cache.flight(cache_key) as waited:
                hit, cached = await cache.get(cache_key, record_stats=False)
                if hit:
                    if waited:
                        state.policy.record_coalesced(1)
                        context.coalesced_requests.append(
                            CoalescedRequestRecord(
                                request_id=f"cache:{request_fingerprint}",
                                request_indexes=list(request_indexes),
                                request_fingerprint=request_fingerprint,
                            )
                        )
                    return cached
                result = await execute_provider()
                await cache.put(cache_key, result)
        else:
            result = await execute_provider()
        response_fingerprint = self.fingerprint(result)
        for record in reversed(records):
            if record.request_id == request_id and record.status == "success":
                record.response_fingerprint = response_fingerprint
                break
        return result
