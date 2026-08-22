from __future__ import annotations

from fastapi import APIRouter, HTTPException

from opensac.api.runtime import ApplicationRuntime
from opensac.models import ExecCreate, ExecResult


def create_execution_router(runtime: ApplicationRuntime) -> APIRouter:
    router = APIRouter()

    @router.post("/v1/sessions/{session_id}/exec", response_model=ExecResult)
    async def execute_code(session_id: str, request: ExecCreate) -> ExecResult:
        """Run one harness-authored program against this session's sandbox."""
        try:
            return await runtime.execute_code(session_id, request)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Session not found") from exc

    return router
