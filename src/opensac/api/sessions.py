from __future__ import annotations

from fastapi import APIRouter, HTTPException

from opensac.api.errors import (
    SessionClosingError,
    SessionExpiredError,
    SessionLostError,
)
from opensac.api.runtime import ApplicationRuntime
from opensac.models import (
    PublicSession,
    Session,
    SessionCreate,
    WorkspaceSnapshot,
    budget_remaining,
)


class SessionRoutes:
    def __init__(self, runtime: ApplicationRuntime) -> None:
        self.runtime = runtime

    def get_session(self, session_id: str) -> Session:
        try:
            return self.runtime.get_session(session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Session not found") from exc

    async def renew(self, session_id: str, *, allow_closing: bool = False) -> Session:
        self.get_session(session_id)
        try:
            return await self.runtime.renew_session(session_id)
        except SessionClosingError:
            if not allow_closing:
                raise
            return self.get_session(session_id)
        except (SessionLostError, SessionExpiredError, KeyError):
            # The lease or lifecycle may have changed while acquiring the
            # session lock. Re-read so the caller receives the stable contract.
            return self.get_session(session_id)

    def public(self, session: Session) -> PublicSession:
        capabilities = session.mechanisms.capabilities()
        if not self.runtime.settings.model_name:
            capabilities = [method for method in capabilities if not method.startswith("llm.")]
        features = [
            "capability_contract_v8",
            "content_passages_v1",
            "provider_reliability_v1",
            "typed_partial_failures_v1",
            "content_grep_report_v1",
            "direct_web_content_v1",
            "lightweight_url_citations_v1",
            "intra_call_dedupe_v1",
            "execution_cancellation_v1",
            "idempotent_exec",
            "worker_affinity",
            "idempotent_session_create",
            "leases",
            "resource_budgets",
            "abort_session",
        ]
        if self.runtime.settings.provider_inflight_coalescing:
            features.append("inflight_coalescing_v1")
        if self.runtime.settings.provider_result_cache_ttl_seconds > 0:
            features.append("provider_result_cache_v1")
        return PublicSession.model_validate(
            {
                **session.model_dump(exclude={"token", "workspace"}),
                "capabilities": capabilities,
                "features": features,
                "budget_remaining": budget_remaining(session.budget, session.usage),
                "state": self.runtime.session_state(session),
            }
        )


def create_session_router(runtime: ApplicationRuntime) -> APIRouter:
    router = APIRouter()
    routes = SessionRoutes(runtime)

    @router.post("/v1/sessions", response_model=PublicSession)
    async def create_session(request: SessionCreate) -> PublicSession:
        session, _ = await runtime.create_session(request)
        return routes.public(session)

    @router.get("/v1/sessions/{session_id}", response_model=PublicSession)
    async def read_session(session_id: str) -> PublicSession:
        return routes.public(await routes.renew(session_id, allow_closing=True))

    @router.post("/v1/sessions/{session_id}/heartbeat", response_model=PublicSession)
    async def heartbeat_session(session_id: str) -> PublicSession:
        return routes.public(await routes.renew(session_id))

    @router.get("/v1/sessions/{session_id}/workspace", response_model=WorkspaceSnapshot)
    async def read_workspace(
        session_id: str,
        max_total_bytes: int = 200_000,
        max_file_bytes: int = 50_000,
    ) -> WorkspaceSnapshot:
        """Read the workspace back before the session is deleted."""
        session = await routes.renew(session_id, allow_closing=True)
        return runtime.store.snapshot_workspace(
            session,
            max_total_bytes=max(max_total_bytes, 0),
            max_file_bytes=max(max_file_bytes, 0),
        )

    @router.delete("/v1/sessions/{session_id}")
    async def delete_session(session_id: str) -> dict[str, str]:
        try:
            deleted = await runtime.close_session(session_id)
        except KeyError:
            deleted = False
        return {"status": "deleted" if deleted else "gone"}

    @router.post("/v1/sessions/{session_id}/abort")
    async def abort_session(session_id: str) -> dict[str, str]:
        deleted = await runtime.abort_session(session_id)
        return {"status": "aborted" if deleted else "gone"}

    return router
