from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from opensac.broker.call_context import current_call
from opensac.broker.session import BrokerSession
from opensac.models import CoalescedRequestRecord, DeduplicatedRequestRecord, ProviderAttemptRecord
from opensac.provider import ProviderAttempt, ProviderRequestError, ProviderRuntime, ProviderWait

from .cache import ProviderResultCache
from .config import ProviderExecutionConfig
from .flights import ProviderFlightCoordinator


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
            attempts=(int(failure.get("attempts") or 0) if attempts is None else attempts),
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
            (failure for failure in failures if not bool(failure.get("retryable"))),
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


class ProviderExecutor:
    """Run, coalesce, trace, and cancel backend provider operations."""

    def __init__(
        self,
        sessions: dict[str, BrokerSession],
        provider_runtime: ProviderRuntime,
        *,
        config: ProviderExecutionConfig,
    ) -> None:
        self.sessions = sessions
        self.provider_runtime = provider_runtime
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
        }
        if error.provider_status is not None:
            failure["provider_status"] = error.provider_status
        if error.retry_after_seconds is not None:
            failure["retry_after_seconds"] = error.retry_after_seconds
        return failure

    @staticmethod
    def is_systemic_search_failure(failure: dict[str, Any]) -> bool:
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
    def is_systemic_content_failure(failure: dict[str, Any]) -> bool:
        """Whether every document failing means the content service is down.

        Permanent document and unexpected non-retryable HTTP failures remain
        aligned rows: unlike a shared outage, they may be specific to the URL
        or corpus entry the caller asked to fetch.
        """

        return str(failure.get("code") or "") in {
            "provider_rate_limited",
            "provider_unavailable",
            "provider_auth_failed",
        }

    async def run(
        self,
        state: BrokerSession,
        *,
        backend: Any,
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
        request_fingerprint = self.fingerprint(request_value)
        context = current_call()
        records = context.provider_attempts if context is not None else None
        trace_buffer = context.provider_trace if context is not None else None

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
        cache = self.result_cache
        cache_enabled = cache.enabled_for(operation)
        cache_key = cache.key(provider_identity, operation, request_fingerprint)
        if cache_enabled:
            hit, cached = await cache.get(cache_key)
            if hit:
                state.policy.record_provider_cache(hit=True)
                if context is not None:
                    context.provider_cache_hits += 1
                return cached
            state.policy.record_provider_cache(hit=False)
            if context is not None:
                context.provider_cache_misses += 1

        async def execute_provider() -> Any:
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
            session_token = context.session_token if context is not None else None
            if session_token and track_execution:
                self.track_execution_task(
                    session_token,
                    context.execution_id,
                    task,
                )
            return await task

        if cache_enabled:
            async with cache.flight(cache_key) as waited:
                hit, cached = await cache.get(cache_key, record_stats=False)
                if hit:
                    if waited:
                        state.policy.record_coalesced(1)
                        if context is not None:
                            context.coalesced_requests.append(
                                CoalescedRequestRecord(
                                    operation_id=f"cache:{request_fingerprint}",
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
        if records is not None:
            for record in reversed(records):
                if record.operation_id == operation_id and record.status == "success":
                    record.response_fingerprint = response_fingerprint
                    break
        return result
