from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request

from opensac import __version__
from opensac.api.errors import install_exception_handlers
from opensac.api.routes import create_api_router
from opensac.api.runtime import ApplicationRuntime
from opensac.config import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    runtime = ApplicationRuntime(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        try:
            await runtime.start()
            yield
        finally:
            await runtime.stop()

    async def authorize(authorization: str | None = Header(default=None)) -> None:
        if settings.api_key and authorization != f"Bearer {settings.api_key}":
            raise HTTPException(status_code=401, detail="Invalid API key")

    app = FastAPI(title="OpenSAC", version=__version__, lifespan=lifespan)
    app.state.runtime = runtime
    install_exception_handlers(app)
    app.include_router(create_api_router(runtime, authorize))

    @app.middleware("http")
    async def worker_identity_header(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-OpenSAC-Worker-ID"] = runtime.worker_id
        response.headers["X-OpenSAC-Worker-Epoch"] = runtime.worker_epoch
        return response

    return app


__all__ = ["ApplicationRuntime", "create_app"]
