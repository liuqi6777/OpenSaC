from __future__ import annotations

import asyncio
import json
import secrets
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from openai import AsyncOpenAI

from opensac.agent import AgentController
from opensac.backends import LocalSearchBackend, PerplexityBackend
from opensac.broker import BrokerRuntime, BrokerService
from opensac.config import Settings
from opensac.models import (
    PublicRun,
    PublicSession,
    Run,
    RunCreate,
    RunStatus,
    Session,
    SessionCreate,
)
from opensac.sandbox import DockerSandbox
from opensac.store import StateStore


class ApplicationRuntime:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.store = StateStore(settings.data_dir)
        self.model_client = AsyncOpenAI(
            api_key=settings.model_api_key or "not-configured",
            base_url=settings.model_base_url,
        )
        self.broker = BrokerService(
            {
                "local": LocalSearchBackend(settings.local_search_base_url),
                "web": PerplexityBackend(settings.perplexity_api_key),
            },
            model_client=self.model_client if settings.model_name else None,
            extraction_model=settings.model_name,
            max_concurrency=settings.max_concurrency,
        )
        self.broker_runtime = BrokerRuntime(self.broker, settings.broker_socket)
        self.sandbox = DockerSandbox(
            image=settings.sandbox_image,
            broker_socket=settings.broker_socket,
            timeout_seconds=settings.sandbox_timeout_seconds,
            memory=settings.sandbox_memory,
            cpus=settings.sandbox_cpus,
            pids_limit=settings.sandbox_pids_limit,
            max_output_bytes=settings.max_output_bytes,
        )
        self.controller = AgentController(
            self.model_client,
            self.sandbox,
            default_model=settings.model_name,
            temperature=settings.model_temperature,
        )
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.events: dict[str, set[asyncio.Queue[dict]]] = defaultdict(set)

    async def start(self) -> None:
        await self.broker_runtime.start()

    async def stop(self) -> None:
        for task in self.tasks.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        await self.broker_runtime.stop()
        await self.model_client.close()

    async def publish(self, run_id: str, event_type: str, data: dict) -> None:
        event = {"type": event_type, "data": data}
        for queue in tuple(self.events[run_id]):
            await queue.put(event)

    async def execute_run(self, run: Run, session: Session) -> None:
        run_token = secrets.token_urlsafe(32)
        state = self.broker.register_session(session, token=run_token)
        try:
            await self.publish(run.id, "run.started", {"run_id": run.id})
            if not self.settings.model_name and not run.model:
                run.status = RunStatus.FAILED
                run.error = "No control model is configured"
            else:
                await self.controller.execute(
                    run,
                    workspace=Path(session.workspace),
                    session_token=run_token,
                    max_turns=session.limits.max_turns,
                )
            run.usage.search_calls = state.policy.usage.search_calls
            run.usage.llm_calls = state.policy.usage.llm_calls
            self.store.save_run(run)
            event_type = "run.completed" if run.status == RunStatus.COMPLETED else "run.failed"
            await self.publish(run.id, event_type, {"run_id": run.id, "status": run.status})
        finally:
            self.broker.unregister_session(run_token)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    runtime = ApplicationRuntime(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await runtime.start()
        yield
        await runtime.stop()

    app = FastAPI(title="OpenSAC", version="0.1.0", lifespan=lifespan)
    app.state.runtime = runtime

    async def authorize(authorization: str | None = Header(default=None)) -> None:
        if not settings.api_key:
            return
        if authorization != f"Bearer {settings.api_key}":
            raise HTTPException(status_code=401, detail="Invalid API key")

    def get_session(session_id: str) -> Session:
        try:
            return runtime.store.get_session(session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Session not found") from exc

    def get_run(run_id: str) -> Run:
        try:
            return runtime.store.get_run(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Run not found") from exc

    def public_session(session: Session) -> PublicSession:
        return PublicSession.model_validate(session.model_dump(exclude={"token", "workspace"}))

    def public_run(run: Run) -> PublicRun:
        session = get_session(run.session_id)
        return PublicRun(
            **run.model_dump(exclude={"trace"}),
            trace=run.trace if run.include_trace else None,
            artifacts=runtime.store.artifacts(session),
            events_url=f"/v1/runs/{run.id}/events",
        )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/sessions", response_model=PublicSession, dependencies=[Depends(authorize)])
    async def create_session(request: SessionCreate) -> PublicSession:
        unknown = set(request.backends) - {"web", "local"}
        if unknown:
            raise HTTPException(status_code=422, detail=f"Unknown backends: {sorted(unknown)}")
        session = runtime.store.create_session(request)
        return public_session(session)

    @app.get(
        "/v1/sessions/{session_id}",
        response_model=PublicSession,
        dependencies=[Depends(authorize)],
    )
    async def read_session(session_id: str) -> PublicSession:
        return public_session(get_session(session_id))

    @app.delete("/v1/sessions/{session_id}", dependencies=[Depends(authorize)])
    async def delete_session(session_id: str) -> dict[str, str]:
        session = get_session(session_id)
        runtime.broker.unregister_session(session.token)
        runtime.store.delete_session(session_id)
        return {"status": "deleted"}

    @app.post(
        "/v1/sessions/{session_id}/runs",
        response_model=PublicRun,
        dependencies=[Depends(authorize)],
    )
    async def create_run(session_id: str, request: RunCreate) -> PublicRun:
        session = get_session(session_id)
        run = runtime.store.create_run(session_id, request)
        task = asyncio.create_task(runtime.execute_run(run, session))
        runtime.tasks[run.id] = task
        return public_run(run)

    @app.get("/v1/runs/{run_id}", response_model=PublicRun, dependencies=[Depends(authorize)])
    async def read_run(run_id: str) -> PublicRun:
        return public_run(get_run(run_id))

    @app.post("/v1/runs/{run_id}/cancel", dependencies=[Depends(authorize)])
    async def cancel_run(run_id: str) -> PublicRun:
        run = get_run(run_id)
        task = runtime.tasks.get(run_id)
        if task and not task.done():
            task.cancel()
            run.status = RunStatus.CANCELLED
            runtime.store.save_run(run)
            await runtime.publish(run.id, "run.cancelled", {"run_id": run.id})
        return public_run(run)

    @app.get("/v1/runs/{run_id}/events", dependencies=[Depends(authorize)])
    async def stream_events(run_id: str, request: Request) -> StreamingResponse:
        run = get_run(run_id)

        async def generate() -> AsyncIterator[str]:
            queue: asyncio.Queue[dict] = asyncio.Queue()
            runtime.events[run_id].add(queue)
            try:
                current = runtime.store.get_run(run_id)
                yield "event: snapshot\ndata: " + current.model_dump_json() + "\n\n"
                if current.status in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}:
                    return
                while not await request.is_disconnected():
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=15)
                    except TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    yield f"event: {event['type']}\ndata: {json.dumps(event['data'])}\n\n"
                    if event["type"] in {"run.completed", "run.failed", "run.cancelled"}:
                        return
            finally:
                runtime.events[run_id].discard(queue)

        del run
        return StreamingResponse(generate(), media_type="text/event-stream")

    @app.get("/v1/runs/{run_id}/artifacts/{artifact_path:path}", dependencies=[Depends(authorize)])
    async def read_artifact(run_id: str, artifact_path: str) -> FileResponse:
        run = get_run(run_id)
        session = get_session(run.session_id)
        try:
            path = runtime.store.read_artifact(session, artifact_path)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Artifact not found") from exc
        return FileResponse(path)

    return app
