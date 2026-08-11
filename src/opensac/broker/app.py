from __future__ import annotations

from fastapi import FastAPI, Header, HTTPException
from opensac_sdk.models import RpcError, RpcRequest, RpcResponse

from opensac.broker.policy import BudgetExceeded, MechanismDisabled
from opensac.broker.service import BrokerService


def _rpc_error(exc: Exception) -> RpcError:
    """Translate broker exceptions into the typed capability wire shape."""
    code = getattr(exc, "code", None)
    retryable = getattr(exc, "retryable", None)
    if isinstance(code, str) and isinstance(retryable, bool):
        return RpcError(
            code=code,
            message=str(exc),
            retryable=retryable,
            attempts=getattr(exc, "attempts", None),
            provider_status=getattr(exc, "provider_status", None),
            retry_after_seconds=getattr(exc, "retry_after_seconds", None),
        )
    if isinstance(exc, BudgetExceeded):
        return RpcError(code="budget_exhausted", message=str(exc), retryable=False)
    if isinstance(exc, MechanismDisabled):
        return RpcError(code="capability_disabled", message=str(exc), retryable=False)
    if isinstance(exc, ValueError):
        return RpcError(code="invalid_request", message=str(exc), retryable=False)
    if isinstance(exc, RuntimeError):
        return RpcError(code="capability_error", message=str(exc), retryable=False)
    return RpcError(
        code="internal_error",
        message="The capability broker failed unexpectedly.",
        retryable=True,
    )


def create_broker_app(service: BrokerService) -> FastAPI:
    app = FastAPI(title="OpenSAC Capability Broker", docs_url=None, redoc_url=None)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/call", response_model=RpcResponse)
    async def call(
        request: RpcRequest,
        authorization: str | None = Header(default=None),
        x_opensac_execution_id: str | None = Header(default=None),
    ) -> RpcResponse:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Missing bearer token")
        token = authorization.removeprefix("Bearer ")
        try:
            result = await service.call(
                token,
                request.method,
                request.params,
                execution_id=x_opensac_execution_id,
            )
            return RpcResponse(ok=True, result=result)
        except PermissionError as exc:
            return RpcResponse(
                ok=False,
                error=RpcError(
                    code="permission_denied",
                    message=str(exc),
                    retryable=False,
                ),
            )
        except Exception as exc:
            return RpcResponse(ok=False, error=_rpc_error(exc))

    return app
