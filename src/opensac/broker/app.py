from __future__ import annotations

from typing import Any, Self

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field, model_validator

from opensac.broker.failures import CapabilityFailure
from opensac.broker.policy import BudgetExceeded, MechanismDisabled
from opensac.broker.service import BrokerService
from opensac.models import CAPABILITY_CONTRACT


class RpcRequest(BaseModel):
    method: str
    params: dict[str, Any] = Field(default_factory=dict)


class RpcResponse(BaseModel):
    """Transport envelope separating top-level errors from successful results."""

    capability_contract: int = CAPABILITY_CONTRACT
    ok: bool
    result: Any = None
    error: CapabilityFailure | None = None

    @model_validator(mode="after")
    def _validate_envelope(self) -> Self:
        if self.ok:
            if self.error is not None:
                raise ValueError("successful RPC responses cannot contain an error")
        elif self.error is None:
            raise ValueError("failed RPC responses must contain an error")
        elif self.result is not None:
            raise ValueError("failed RPC responses cannot contain a result")
        return self


def _rpc_error(exc: Exception) -> CapabilityFailure:
    """Translate broker exceptions into the typed capability wire shape."""
    code = getattr(exc, "code", None)
    retryable = getattr(exc, "retryable", None)
    if isinstance(code, str) and isinstance(retryable, bool):
        return CapabilityFailure(
            code=code,
            message=str(exc),
            retryable=retryable,
            attempts=getattr(exc, "attempts", None) or 0,
            provider_status=getattr(exc, "provider_status", None),
            retry_after_seconds=getattr(exc, "retry_after_seconds", None),
            provider=getattr(exc, "provider", None),
            component=getattr(exc, "component", None),
            scope=getattr(exc, "scope", None),
        )
    if isinstance(exc, BudgetExceeded):
        return CapabilityFailure(code="budget_exhausted", message=str(exc), retryable=False)
    if isinstance(exc, MechanismDisabled):
        return CapabilityFailure(code="capability_disabled", message=str(exc), retryable=False)
    if isinstance(exc, ValueError):
        return CapabilityFailure(code="invalid_request", message=str(exc), retryable=False)
    if isinstance(exc, RuntimeError):
        return CapabilityFailure(code="capability_error", message=str(exc), retryable=False)
    return CapabilityFailure(
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
        x_opensac_capability_contract: str | None = Header(default=None),
        x_opensac_execution_id: str | None = Header(default=None),
    ) -> RpcResponse:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Missing bearer token")
        expected_contract = str(CAPABILITY_CONTRACT)
        if x_opensac_capability_contract != expected_contract:
            reported = x_opensac_capability_contract or "missing"
            return RpcResponse(
                ok=False,
                error=CapabilityFailure(
                    code="capability_contract_mismatch",
                    message=(
                        f"Capability contract mismatch: broker requires {expected_contract}, "
                        f"client reported {reported}. Deploy matching OpenSAC SDK, broker, "
                        "and sandbox versions."
                    ),
                    retryable=False,
                ),
            )
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
                error=CapabilityFailure(
                    code="permission_denied",
                    message=str(exc),
                    retryable=False,
                ),
            )
        except Exception as exc:
            return RpcResponse(ok=False, error=_rpc_error(exc))

    return app
