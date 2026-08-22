from __future__ import annotations

import asyncio
import copy
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
from opensac.models import CoalescedRequestRecord

from .config import ProviderExecutionConfig


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


class ProviderFlightCoordinator:
    """Own session-scoped provider coalescing and waiter cancellation."""

    def __init__(self, config: ProviderExecutionConfig) -> None:
        self.enabled = config.inflight_coalescing
        self.max_keys = config.max_inflight_keys
        self.max_waiters_per_key = config.max_waiters_per_flight

    @staticmethod
    def key(operation: str, request_fingerprint: str) -> str:
        return f"{operation}:{request_fingerprint}"

    async def admit(
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

        if not self.enabled or not requests:
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
                if entry.waiters >= self.max_waiters_per_key:
                    raise InflightCapacityError()
                attached.append((key, entry, fingerprint, request_indexes))

            if len(state.flights) + len(new_keys) > self.max_keys:
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
                entry.future.add_done_callback(self._consume_future)
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

    def start(
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
    def _consume_future(future: asyncio.Future[Any]) -> None:
        """Suppress unobserved-exception warnings after every waiter detaches."""

        if not future.cancelled():
            future.exception()

    async def wait(self, state: BrokerSession, waiter: FlightWaiter) -> Any:
        try:
            # A waiter cancellation is only a detach. Shielding prevents it
            # from cancelling the shared future that other capability calls
            # are still waiting on.
            result = await asyncio.shield(waiter.entry.future)
            return copy.deepcopy(result)
        finally:
            await self.detach(state, waiter)

    async def detach(self, state: BrokerSession, waiter: FlightWaiter) -> None:
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

    async def detach_execution(self, state: BrokerSession, execution_id: str) -> None:
        async with state.flight_lock:
            waiters = list(state.flight_waiters_by_execution.get(execution_id, set()))
        await asyncio.gather(*(self.detach(state, waiter) for waiter in waiters))

    async def cancel_all(self, state: BrokerSession) -> int:
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
