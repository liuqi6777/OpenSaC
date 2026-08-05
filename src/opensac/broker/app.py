from __future__ import annotations

from fastapi import FastAPI, Header, HTTPException
from opensac_sdk.models import RpcRequest, RpcResponse

from opensac.broker.service import BrokerService


def create_broker_app(service: BrokerService) -> FastAPI:
    app = FastAPI(title="OpenSAC Capability Broker", docs_url=None, redoc_url=None)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/call", response_model=RpcResponse)
    async def call(
        request: RpcRequest,
        authorization: str | None = Header(default=None),
    ) -> RpcResponse:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Missing bearer token")
        token = authorization.removeprefix("Bearer ")
        try:
            result = await service.call(token, request.method, request.params)
            return RpcResponse(ok=True, result=result)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except Exception as exc:
            return RpcResponse(ok=False, error=str(exc))

    return app
