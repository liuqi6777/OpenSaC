from __future__ import annotations

import asyncio

from opensac.metrics import CapacityGate, CapacityLimitedSandbox
from opensac.sandbox import SandboxRequest, SandboxResult


async def test_capacity_gate_reports_queue_depth_and_wait_time() -> None:
    gate = CapacityGate(1)
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    waits: list[float] = []

    async def first() -> None:
        async with gate.slot() as wait:
            waits.append(wait)
            first_entered.set()
            await release_first.wait()

    async def second() -> None:
        await first_entered.wait()
        async with gate.slot() as wait:
            waits.append(wait)

    first_task = asyncio.create_task(first())
    second_task = asyncio.create_task(second())
    await first_entered.wait()
    await asyncio.sleep(0)
    assert gate.snapshot() == {"capacity": 1, "active": 1, "waiting": 1, "admitted": 1}

    release_first.set()
    await asyncio.gather(first_task, second_task)

    assert waits[0] >= 0
    assert waits[1] > 0
    assert gate.snapshot() == {"capacity": 1, "active": 0, "waiting": 0, "admitted": 2}


async def test_capacity_limited_sandbox_records_queue_wait(tmp_path) -> None:
    class FakeSandbox:
        async def execute(self, _request: SandboxRequest) -> SandboxResult:
            return SandboxResult(0, "", "", 0.1)

    sandbox = CapacityLimitedSandbox(FakeSandbox(), CapacityGate(1))
    result = await sandbox.execute(SandboxRequest("pass", tmp_path, "token"))
    assert result.timings["sandbox_queue_seconds"] >= 0
