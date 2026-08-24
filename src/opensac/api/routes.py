from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Depends

from opensac.api.execution import create_execution_router
from opensac.api.runtime import ApplicationRuntime
from opensac.api.sessions import create_session_router
from opensac.models import utc_now

Authorize = Callable[..., Awaitable[None]]


def create_api_router(runtime: ApplicationRuntime, authorize: Authorize) -> APIRouter:
    router = APIRouter()
    protected = APIRouter(dependencies=[Depends(authorize)])

    @router.get("/healthz")
    async def healthz() -> dict[str, Any]:
        sessions = runtime.store.sessions()
        current_time = utc_now()
        active_sessions = [
            item
            for item in sessions
            if not item.closing and not runtime._is_expired(item, current_time)
        ]
        warm_snapshot = getattr(runtime.sandbox, "snapshot", None)
        return {
            "status": "ok",
            "worker_id": runtime.worker_id,
            "worker_epoch": runtime.worker_epoch,
            "state": "accepting" if runtime.accepting else "draining",
            "accepting": runtime.accepting,
            "build": runtime.environment_manifest(),
            "process": runtime.process_snapshot(),
            "sandbox_mode": runtime.settings.sandbox_mode,
            "sandbox": runtime.sandbox_gate.snapshot(),
            "warm": warm_snapshot() if callable(warm_snapshot) else None,
            "persistent_interpreter": (
                runtime.persistent_sandbox.snapshot()
                if runtime.settings.experimental_persistent_interpreter
                and runtime.persistent_sandbox is not None
                else None
            ),
            "broker": runtime.broker.capacity_gate.snapshot(),
            "provider_cache": runtime.broker.providers.result_cache.snapshot(),
            "sessions": {
                "capacity": runtime.settings.max_active_sessions,
                "active": len(active_sessions),
                "waiting": 0,
                "leased": sum(item.lease_expires_at is not None for item in active_sessions),
                "executing": sum(
                    bool(runtime._active_session_tasks(item.id)) for item in active_sessions
                ),
            },
            "inflight_execs": len(runtime.exec_tasks),
        }

    @protected.post("/v1/admin/drain")
    async def drain_worker() -> dict[str, str]:
        runtime.accepting = False
        return {"status": "draining", "worker_id": runtime.worker_id}

    protected.include_router(create_session_router(runtime))
    protected.include_router(create_execution_router(runtime))
    router.include_router(protected)
    return router
