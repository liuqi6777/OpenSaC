from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from opensac.broker.call_context import current_call
from opensac.broker.session import (
    BrokerSession,
    FlightAdmission,
    FlightEntry,
    FlightGroup,
    FlightWaiter,
)
from opensac.models import CoalescedRequestRecord, DeduplicatedRequestRecord, ProviderAttemptRecord
from opensac.provider import ProviderAttempt, ProviderRequestError, ProviderRuntime, ProviderWait


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


class ProviderExecutor:
    """Run, coalesce, trace, and cancel backend provider operations."""

    def __init__(
        self,
        sessions: dict[str, BrokerSession],
        provider_runtime: ProviderRuntime,
        *,
        inflight_coalescing: bool,
        max_inflight_keys: int,
        max_waiters_per_flight: int,
    ) -> None:
        self.sessions = sessions
        self.provider_runtime = provider_runtime
        self.inflight_coalescing = inflight_coalescing
        self.max_inflight_keys = max_inflight_keys
        self.max_waiters_per_flight = max_waiters_per_flight
        self.execution_tasks: dict[tuple[str, str], set[asyncio.Task[Any]]] = {}

    async def aclose(self) -> None:
        await asyncio.gather(*(self.cancel_all_flights(state) for state in self.sessions.values()))
        pending = {
            task for tasks in self.execution_tasks.values() for task in tasks if not task.done()
        }
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self.execution_tasks.clear()

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
            async with state.flight_lock:
                flight_waiters = list(state.flight_waiters_by_execution.get(execution_id, set()))
            await asyncio.gather(
                *(self.detach_flight_waiter(state, waiter) for waiter in flight_waiters)
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
            cancelled_flights = await self.cancel_all_flights(state)
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
    def flight_key(operation: str, request_fingerprint: str) -> str:
        return f"{operation}:{request_fingerprint}"

    async def admit_flights(
        self,
        state: BrokerSession,
        requests: dict[str, tuple[str, list[int]]],
        *,
        group_new: bool,
    ) -> FlightAdmission:
        """Atomically attach or lead every unique key in one capability call.

        ``requests`` is already deduplicated within the call. Consequently one
        key consumes one waiter even if several logical rows map to it. The
        full validation happens under one lock before any entry or waiter is
        added, so a capacity error cannot leave a partial attachment behind.
        """

        if not self.inflight_coalescing or not requests:
            return FlightAdmission(waiters={}, new_groups=[])

        context = current_call()
        execution_id = context.execution_id if context is not None else None
        attached: list[tuple[str, FlightEntry, str, list[int]]] = []
        created: list[tuple[str, FlightEntry, str, list[int]]] = []
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

            new_groups: list[FlightGroup] = []
            shared_group: FlightGroup | None = None
            if new_keys and group_new:
                shared_group = FlightGroup(operation_id=f"op_{uuid.uuid4().hex}")
                new_groups.append(shared_group)
            for key in new_keys:
                fingerprint, request_indexes = requests[key]
                group = shared_group
                if group is None:
                    group = FlightGroup(operation_id=f"op_{uuid.uuid4().hex}")
                    new_groups.append(group)
                entry = FlightEntry(
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

            waiters: dict[str, FlightWaiter] = {}
            for key, entry, _fingerprint, _request_indexes in [*attached, *created]:
                entry.waiters += 1
                waiter = FlightWaiter(
                    key=key,
                    entry=entry,
                    execution_id=execution_id,
                )
                waiters[key] = waiter
                if execution_id:
                    state.flight_waiters_by_execution.setdefault(execution_id, set()).add(waiter)

        if attached:
            state.policy.record_coalesced(len(attached))
            if context is not None:
                context.coalesced_requests.extend(
                    CoalescedRequestRecord(
                        operation_id=entry.operation_id,
                        request_indexes=list(request_indexes),
                        request_fingerprint=fingerprint,
                    )
                    for _key, entry, fingerprint, request_indexes in attached
                )
        return FlightAdmission(waiters=waiters, new_groups=new_groups)

    def start_flight_group(
        self,
        state: BrokerSession,
        group: FlightGroup,
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
                        raise RuntimeError("in-flight transport group returned an invalid key set")
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

    async def await_flight(
        self,
        state: BrokerSession,
        waiter: FlightWaiter,
    ) -> Any:
        try:
            # A waiter cancellation is only a detach. Shielding prevents it
            # from cancelling the shared future that other capability calls
            # are still waiting on.
            result = await asyncio.shield(waiter.entry.future)
            return copy.deepcopy(result)
        finally:
            await self.detach_flight_waiter(state, waiter)

    async def detach_flight_waiter(
        self,
        state: BrokerSession,
        waiter: FlightWaiter,
    ) -> None:
        cancel_task: asyncio.Task[None] | None = None
        async with state.flight_lock:
            if not waiter.active:
                return
            waiter.active = False
            entry = waiter.entry
            entry.waiters = max(0, entry.waiters - 1)
            if waiter.execution_id:
                execution_waiters = state.flight_waiters_by_execution.get(waiter.execution_id)
                if execution_waiters is not None:
                    execution_waiters.discard(waiter)
                    if not execution_waiters:
                        state.flight_waiters_by_execution.pop(waiter.execution_id, None)
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

    async def cancel_all_flights(self, state: BrokerSession) -> int:
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
            "provider_timeout",
            "provider_rate_limited",
            "provider_unavailable",
            "provider_auth_failed",
            "provider_invalid_response",
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
        result = await task
        response_fingerprint = self.fingerprint(result)
        if records is not None:
            for record in reversed(records):
                if record.operation_id == operation_id and record.status == "success":
                    record.response_fingerprint = response_fingerprint
                    break
        return result
