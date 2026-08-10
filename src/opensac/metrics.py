from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from opensac.sandbox.base import Sandbox, SandboxRequest, SandboxResult


class CapacityGate:
    """A semaphore that exposes queue depth and reports each caller's wait."""

    def __init__(self, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("capacity must be at least 1")
        self.capacity = capacity
        self._semaphore = asyncio.Semaphore(capacity)
        self.active = 0
        self.waiting = 0
        self.admitted = 0

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[float]:
        queued_at = time.monotonic()
        self.waiting += 1
        try:
            await self._semaphore.acquire()
        except BaseException:
            self.waiting -= 1
            raise
        self.waiting -= 1
        self.active += 1
        self.admitted += 1
        try:
            yield time.monotonic() - queued_at
        finally:
            self.active -= 1
            self._semaphore.release()

    def snapshot(self) -> dict[str, int]:
        return {
            "capacity": self.capacity,
            "active": self.active,
            "waiting": self.waiting,
            "admitted": self.admitted,
        }


class CapacityLimitedSandbox:
    """Apply the global execution ceiling to sandbox callers outside /exec."""

    def __init__(self, sandbox: Sandbox, gate: CapacityGate) -> None:
        self.sandbox = sandbox
        self.gate = gate

    async def execute(self, request: SandboxRequest) -> SandboxResult:
        async with self.gate.slot() as queue_seconds:
            result = await self.sandbox.execute(request)
        result.timings.setdefault("sandbox_queue_seconds", queue_seconds)
        return result
