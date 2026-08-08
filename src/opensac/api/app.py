from __future__ import annotations

import asyncio
import json
import secrets
import tempfile
import uuid
from collections import Counter, defaultdict
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from openai import AsyncOpenAI

from opensac.agent import AgentController
from opensac.backends import LocalSearchBackend, SerperBackend
from opensac.broker import BrokerRuntime, BrokerService, resolve_broker_socket_path
from opensac.broker.service import BrokerSession
from opensac.config import Settings
from opensac.models import (
    CapabilityEvent,
    ExecCreate,
    ExecResult,
    ProgramRecord,
    PublicRun,
    PublicSession,
    Run,
    RunCreate,
    RunStatus,
    RunUsage,
    Session,
    SessionCreate,
    WorkspaceSnapshot,
)
from opensac.sandbox import DockerSandbox, UnsafeCodeError
from opensac.sandbox.base import SandboxRequest, SandboxResult
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
                "local": LocalSearchBackend(
                    settings.local_search_base_url,
                    fetch_concurrency=settings.backend_fetch_concurrency,
                ),
                "web": SerperBackend(
                    settings.serper_api_key,
                    fetch_concurrency=settings.backend_fetch_concurrency,
                ),
            },
            model_client=self.model_client if settings.model_name else None,
            extraction_model=settings.model_name,
            max_concurrency=settings.max_concurrency,
            max_context_payload_bytes=settings.max_context_payload_bytes,
            session_content_cache_bytes=settings.session_content_cache_bytes,
        )
        broker_socket = resolve_broker_socket_path(settings.broker_socket)
        self.broker_runtime = BrokerRuntime(self.broker, broker_socket)
        self.sandbox = DockerSandbox(
            image=settings.sandbox_image,
            broker_socket=broker_socket,
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
        # /exec is driven by an external harness that may have dozens of
        # rollouts in flight. Without a ceiling each in-flight tool call would
        # start its own container.
        self.sandbox_gate = asyncio.Semaphore(settings.sandbox_max_concurrency)
        self.session_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

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

    def bind_session(self, session: Session) -> BrokerSession:
        """Attach a long-lived broker state to a session.

        Runs get a throwaway token per run because their capability budget is
        per-run. `/exec` is the opposite: the harness owns the loop, so quotas
        and the search reference table have to survive across calls. Keying the
        broker state on the durable `session.token` gives a program the ability
        to persist refs to its workspace in one turn and resolve them in a
        later one.

        Idempotent, so a session created before a process restart keeps working.
        Note that only the workspace survives such a restart: broker state is in
        memory, so refs minted beforehand come back as unknown references.
        """
        state = self.broker.sessions.get(session.token)
        if state is None:
            state = self.broker.register_session(session)
        return state

    @contextmanager
    def _exec_workspace(self, session: Session) -> Iterator[Path]:
        """The directory this execution runs against.

        With persistence enabled -- the default and the only configuration a
        normal run uses -- this is the session's own workspace, so files written
        in one call are there in the next. Disabled, the program gets a fresh
        directory that is discarded on the way out: it can still write and read
        back within one program, but it cannot carry anything forward, which is
        the property the ablation removes.
        """
        if session.mechanisms.persistence:
            workspace = Path(session.workspace)
            workspace.mkdir(parents=True, exist_ok=True)
            yield workspace
            return
        with tempfile.TemporaryDirectory(prefix="opensac-ephemeral-") as directory:
            yield Path(directory)

    @staticmethod
    def _program_error_category(
        result: SandboxResult | None, *, rejected: bool = False
    ) -> str | None:
        """What went wrong, from what this process alone can see.

        Deliberately coarser than the host-side classifier, which also reads the
        capability trace and can tell a search failure from a fetch failure.
        This one only has to separate the classes that decide whether a program
        ran at all, because those are the ones that must not be read as the
        model failing at the task.
        """
        if rejected:
            return "code_validation"
        if result is None:
            return "sandbox"
        if result.launch_error:
            return "sandbox"
        if result.timed_out:
            return "timeout"
        if result.exit_code != 0:
            return "runtime"
        return None

    async def execute_code(
        self,
        session: Session,
        code: str,
        *,
        include_trace: bool = False,
    ) -> ExecResult:
        state = self.bind_session(session)
        # One execution at a time per session. The workspace, the program
        # archive and the broker's reference table are all session-scoped, and
        # two programs sharing them concurrently is not a configuration anyone
        # asks for -- but it used to be reachable, and it silently corrupted the
        # archive by letting one program's code be recorded against another's
        # result. The ceiling on total containers is a separate, global gate.
        async with self.session_locks[session.id]:
            sequence, program_path = self.store.reserve_program(session, code)
            # An execution id is always minted, not only when the caller wants
            # the trace back: the per-program capability counts come from the
            # same trace, and it is drained unconditionally below, so nothing
            # accumulates for a caller that never asks for it.
            execution_id = uuid.uuid4().hex
            request_names = {
                "program_filename": f".opensac-program-{sequence:03d}.py",
                "output_filename": f".opensac-output-{sequence:03d}.json",
            }
            with self._exec_workspace(session) as workspace:
                result: SandboxResult | None = None
                rejection: str | None = None
                try:
                    async with self.sandbox_gate:
                        result = await self.sandbox.execute(
                            SandboxRequest(
                                code=code,
                                workspace=workspace,
                                session_token=session.token,
                                execution_id=execution_id,
                                **request_names,
                            )
                        )
                except UnsafeCodeError as exc:
                    # A rejection is a normal observation for the control model,
                    # not a transport error: it has to see the reason and
                    # rewrite the program.
                    rejection = f"Rejected by the sandbox code validator: {exc}"

                if result is not None:
                    await state.policy.record_sandbox_seconds(result.duration_seconds)
                trace = self.broker.take_trace(session.token, execution_id)
                artifacts = self.store.artifacts(session, workspace)

            self.store.record_program(
                session,
                ProgramRecord(
                    sequence=sequence,
                    path=str(program_path),
                    code=code,
                    exit_code=result.exit_code if result else -1,
                    timed_out=bool(result.timed_out) if result else False,
                    duration_seconds=result.duration_seconds if result else 0.0,
                    error=rejection or (result.launch_error if result else None),
                    error_category=self._program_error_category(
                        result, rejected=rejection is not None
                    ),
                    stdout_bytes=len(result.stdout.encode()) if result else 0,
                    stderr_bytes=len(result.stderr.encode()) if result else 0,
                    capability_calls=dict(Counter(event.method for event in trace)),
                ),
            )

            return ExecResult(
                exit_code=result.exit_code if result else -1,
                stdout=result.stdout if result else "",
                stderr=result.stderr if result else "",
                duration_seconds=result.duration_seconds if result else 0.0,
                timed_out=bool(result.timed_out) if result else False,
                succeeded=bool(result.succeeded) if result else False,
                output=result.output if result else None,
                citations=result.citations if result else [],
                error=rejection or (result.launch_error if result else None),
                usage=self._session_usage(state),
                artifacts=artifacts,
                trace=self._returned_trace(session, trace, include_trace=include_trace),
            )

    @staticmethod
    def _returned_trace(
        session: Session,
        trace: list[CapabilityEvent],
        *,
        include_trace: bool,
    ) -> list[CapabilityEvent]:
        """What of the trace goes back to the caller.

        A session that disables context decoupling puts its results in the
        trace, and those results are the whole point of that arm -- so the
        caller gets them whether or not it asked for a trace, since a harness
        written against the default would otherwise silently run the ablation
        without receiving what makes it an ablation.
        """
        if include_trace or not session.mechanisms.context_decoupling:
            return trace
        return []

    def _session_usage(self, state: BrokerSession) -> RunUsage:
        return RunUsage.model_validate(state.policy.usage.model_dump())

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
        return PublicSession.model_validate(
            {
                **session.model_dump(exclude={"token", "workspace"}),
                "capabilities": session.mechanisms.capabilities(),
            }
        )

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

    @app.get(
        "/v1/sessions/{session_id}/workspace",
        response_model=WorkspaceSnapshot,
        dependencies=[Depends(authorize)],
    )
    async def read_workspace(
        session_id: str,
        max_total_bytes: int = 200_000,
        max_file_bytes: int = 50_000,
    ) -> WorkspaceSnapshot:
        """Read the workspace back before the session is deleted.

        For the harness archiving a finished rollout, not for the control
        model: nothing here passes through an observation, which is why it is
        a separate request rather than a field on `ExecResult`.
        """
        return runtime.store.snapshot_workspace(
            get_session(session_id),
            max_total_bytes=max(max_total_bytes, 0),
            max_file_bytes=max(max_file_bytes, 0),
        )

    @app.delete("/v1/sessions/{session_id}", dependencies=[Depends(authorize)])
    async def delete_session(session_id: str) -> dict[str, str]:
        session = get_session(session_id)
        runtime.broker.unregister_session(session.token)
        runtime.store.delete_session(session_id)
        runtime.session_locks.pop(session_id, None)
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

    @app.post(
        "/v1/sessions/{session_id}/exec",
        response_model=ExecResult,
        dependencies=[Depends(authorize)],
    )
    async def execute_code(session_id: str, request: ExecCreate) -> ExecResult:
        """Run one harness-authored program against this session's sandbox.

        The caller owns the control loop. OpenSAC contributes the sandbox, the
        SDK, and the broker; it never invokes a control model here.
        """
        session = get_session(session_id)
        return await runtime.execute_code(
            session,
            request.code,
            include_trace=request.include_trace,
        )

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
