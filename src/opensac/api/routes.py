from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import APIRouter, Depends

from opensac.api.execution import create_execution_router
from opensac.api.runtime import ApplicationRuntime
from opensac.api.sessions import create_session_router

Authorize = Callable[..., Awaitable[None]]


def create_api_router(runtime: ApplicationRuntime, authorize: Authorize) -> APIRouter:
    router = APIRouter()
    protected = APIRouter(dependencies=[Depends(authorize)])

    @router.get("/healthz")
    async def healthz() -> dict[str, object]:
        return runtime.health_snapshot()

    @protected.post("/v1/admin/drain")
    async def drain_worker() -> dict[str, str]:
        runtime.accepting = False
        return {"status": "draining", "worker_id": runtime.worker_id}

    protected.include_router(create_session_router(runtime))
    protected.include_router(create_execution_router(runtime))
    router.include_router(protected)
    return router
