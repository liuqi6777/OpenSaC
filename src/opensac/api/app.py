from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import secrets
import tempfile
import time
import uuid
from collections import Counter, defaultdict
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from openai import AsyncOpenAI

from opensac.agent import AgentController
from opensac.backends import LocalSearchBackend, SerperBackend
from opensac.broker import BrokerRuntime, BrokerService, resolve_broker_socket_path
from opensac.broker.service import BrokerSession
from opensac.config import Settings
from opensac.metrics import CapacityGate, CapacityLimitedSandbox
from opensac.models import (
    CapabilityEvent,
    ExecCreate,
    ExecRecord,
    ExecRecordStatus,
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
    utc_now,
)
from opensac.sandbox import DockerSandbox, UnsafeCodeError, WarmDockerSandbox
from opensac.sandbox.base import SandboxRequest, SandboxResult
from opensac.store import StateStore

logger = logging.getLogger(__name__)


class SessionClosingError(RuntimeError):
    pass


class SessionCleanupError(RuntimeError):
    pass


class ExecIdConflictError(RuntimeError):
    pass


class ExecIndeterminateError(RuntimeError):
    pass


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
            max_search_queries_per_request=settings.search_max_queries_per_request,
            max_search_query_chars=settings.search_max_query_chars,
            max_search_top_k=settings.search_max_top_k,
        )
        broker_socket = resolve_broker_socket_path(settings.broker_socket)
        self.broker_runtime = BrokerRuntime(self.broker, broker_socket)
        sandbox_type = WarmDockerSandbox if settings.sandbox_mode == "warm" else DockerSandbox
        sandbox_kwargs = dict(
            image=settings.sandbox_image,
            broker_socket=broker_socket,
            timeout_seconds=settings.sandbox_timeout_seconds,
            memory=settings.sandbox_memory,
            cpus=settings.sandbox_cpus,
            pids_limit=settings.sandbox_pids_limit,
            max_output_bytes=settings.max_output_bytes,
        )
        if sandbox_type is WarmDockerSandbox:
            sandbox_kwargs["idle_timeout_seconds"] = settings.sandbox_warm_idle_seconds
        self.sandbox = sandbox_type(**sandbox_kwargs)
        self.sandbox_gate = CapacityGate(settings.sandbox_max_concurrency)
        self.controller = AgentController(
            self.model_client,
            CapacityLimitedSandbox(self.sandbox, self.sandbox_gate),
            default_model=settings.model_name,
            temperature=settings.model_temperature,
        )
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.events: dict[str, set[asyncio.Queue[dict]]] = defaultdict(set)
        # /exec is driven by an external harness that may have dozens of
        # rollouts in flight. Without a ceiling each in-flight tool call would
        # start its own container.
        self.session_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.closing_sessions: set[str] = set()
        self.exec_tasks: set[asyncio.Task[ExecResult]] = set()
        self.inflight_execs: dict[
            tuple[str, str], tuple[str, asyncio.Task[ExecResult]]
        ] = {}
        # A reservation is registered synchronously when /exec or /runs accepts
        # work, before the child task gets its first event-loop turn. DELETE can
        # then distinguish admitted work from a late request even when the task
        # has not acquired the session lock yet.
        self.session_tasks: dict[str, set[asyncio.Task[Any]]] = defaultdict(set)
        self._reaper_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        await self.broker_runtime.start()
        reap_orphans = getattr(self.sandbox, "reap_orphans", None)
        if callable(reap_orphans):
            await reap_orphans()
        # A process may have died after persisting `closing=true` but before it
        # removed the directory. Finish that cleanup before accepting new work.
        for session in self.store.sessions():
            if session.closing:
                await self.close_session(session.id)
        if self.settings.session_ttl_seconds > 0 or self._warm_reaper_enabled():
            self._reaper_task = asyncio.create_task(self._reaper_loop())

    async def stop(self) -> None:
        try:
            if self._reaper_task is not None:
                self._reaper_task.cancel()
                await asyncio.gather(self._reaper_task, return_exceptions=True)
                self._reaper_task = None
            run_tasks = tuple(self.tasks.items())
            for _, task in run_tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*(task for _, task in run_tasks), return_exceptions=True)
            for run_id, task in run_tasks:
                if not task.cancelled():
                    continue
                try:
                    run = self.store.get_run(run_id)
                except KeyError:
                    continue
                except Exception:
                    logger.exception("shutdown_run_status_read_failed run_id=%s", run_id)
                    continue
                if run.status in {RunStatus.QUEUED, RunStatus.RUNNING}:
                    run.status = RunStatus.CANCELLED
                    run.updated_at = utc_now()
                    try:
                        self.store.save_run(run)
                    except Exception:
                        logger.exception("shutdown_run_status_save_failed run_id=%s", run_id)
            for task in tuple(self.exec_tasks):
                if not task.done():
                    task.cancel()
            await asyncio.gather(*self.exec_tasks, return_exceptions=True)
        finally:
            try:
                await self._close_sandbox()
            finally:
                try:
                    await self.broker_runtime.stop()
                finally:
                    await self.model_client.close()

    def _warm_reaper_enabled(self) -> bool:
        return (
            self.settings.sandbox_warm_idle_seconds > 0
            and callable(getattr(self.sandbox, "reap_idle", None))
        )

    async def _close_sandbox(self) -> None:
        close = getattr(self.sandbox, "close", None)
        if not callable(close):
            return
        try:
            outcome = close()
            if inspect.isawaitable(outcome):
                await outcome
        except Exception:
            logger.exception("sandbox_close_failed")

    async def _reaper_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.session_reaper_interval_seconds)
            try:
                await self.reap_expired_sessions()
                reap_idle = getattr(self.sandbox, "reap_idle", None)
                if self._warm_reaper_enabled() and callable(reap_idle):
                    await reap_idle(self.settings.sandbox_warm_idle_seconds)
            except Exception:
                # One corrupt or concurrently removed session must not disable
                # cleanup for every session created afterwards.
                logger.exception("session_reaper_failed")

    async def publish(self, run_id: str, event_type: str, data: dict) -> None:
        event = {"type": event_type, "data": data}
        for queue in tuple(self.events[run_id]):
            await queue.put(event)

    def _reserve_session_task(self, session_id: str, task: asyncio.Task[Any]) -> None:
        self.session_tasks[session_id].add(task)

        def done(finished: asyncio.Task[Any]) -> None:
            tasks = self.session_tasks.get(session_id)
            if tasks is None:
                return
            tasks.discard(finished)
            if not tasks:
                self.session_tasks.pop(session_id, None)

        task.add_done_callback(done)

    def _active_session_tasks(self, session_id: str) -> tuple[asyncio.Task[Any], ...]:
        return tuple(
            task for task in self.session_tasks.get(session_id, ()) if not task.done()
        )

    def track_run_task(
        self,
        run_id: str,
        session_id: str,
        task: asyncio.Task[None],
    ) -> None:
        self.tasks[run_id] = task
        self._reserve_session_task(session_id, task)

        def done(finished: asyncio.Task[None]) -> None:
            if self.tasks.get(run_id) is finished:
                self.tasks.pop(run_id, None)
            if not finished.cancelled():
                finished.exception()

        task.add_done_callback(done)

    def start_run_task(self, run: Run, session: Session) -> asyncio.Task[None]:
        current = self.store.get_session(session.id)
        if current.closing or session.id in self.closing_sessions:
            raise SessionClosingError(f"Session '{session.id}' is closing")
        task = asyncio.create_task(
            self.execute_run(run, current, admitted=True),
            name=f"opensac-run-{run.id}",
        )
        self.track_run_task(run.id, session.id, task)
        return task

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

    @staticmethod
    def _exec_request_hash(request: ExecCreate) -> str:
        payload = request.model_dump(mode="json", exclude={"exec_id"})
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    async def _close_sandbox_session(self, session: Session) -> None:
        """Optional connection point for a stateful or pooled sandbox backend."""
        close = getattr(self.sandbox, "close_session", None)
        if not callable(close):
            return
        try:
            outcome = close(session)
            if inspect.isawaitable(outcome):
                await outcome
        except Exception as exc:
            logger.exception("sandbox_session_close_failed session_id=%s", session.id)
            raise SessionCleanupError(
                f"Sandbox cleanup failed for session '{session.id}'"
            ) from exc

    async def close_session(
        self,
        session_id: str,
        *,
        idle_before: datetime | None = None,
    ) -> bool:
        """Close one session after any execution already holding its lock.

        `closing` is persisted before waiting. New executions therefore fail
        rather than queueing behind DELETE, and a process restart completes the
        cleanup during startup. `idle_before` makes the operation conditional
        for the TTL reaper and is checked immediately before that transition.
        """
        lock = self.session_locks[session_id]
        # Close the admission gate before inspecting the lock. An execution
        # that already acquired it makes a TTL close back off; one that only
        # passed its first check will observe this gate after acquiring it.
        self.closing_sessions.add(session_id)
        try:
            admitted = self._active_session_tasks(session_id)
            if idle_before is not None and (admitted or lock.locked()):
                return False
            session = self.store.get_session(session_id)
            if (
                idle_before is not None
                and not session.closing
                and session.last_access > idle_before
            ):
                return False
            session = self.store.mark_session_closing(session_id)
            # Explicit DELETE owns the lifecycle transition, but work accepted
            # before it closed the admission gate still owns its result. Wait
            # for every such task without holding the session lock; the tasks
            # may themselves be queued on that lock.
            if admitted:
                await asyncio.gather(*admitted, return_exceptions=True)
            async with lock:
                # Reload after admitted work touched the session. A TTL close
                # never gets here while a reservation or lock holder exists.
                session = self.store.get_session(session_id)
                self.broker.unregister_session(session.token)
                await self._close_sandbox_session(session)
                self.store.delete_session(session_id)
            return True
        finally:
            self.closing_sessions.discard(session_id)
            if not lock.locked() and self.session_locks.get(session_id) is lock:
                self.session_locks.pop(session_id, None)

    async def reap_expired_sessions(self, *, now: datetime | None = None) -> list[str]:
        """Reclaim idle sessions once; the background task calls this periodically."""
        ttl = self.settings.session_ttl_seconds
        if ttl <= 0:
            return []
        cutoff = (now or utc_now()) - timedelta(seconds=ttl)
        removed: list[str] = []
        for session in self.store.sessions():
            if not session.closing and session.last_access > cutoff:
                continue
            try:
                closed = await self.close_session(session.id, idle_before=cutoff)
            except KeyError:
                continue
            except Exception:
                logger.exception("session_reap_failed session_id=%s", session.id)
                continue
            if closed:
                removed.append(session.id)
        return removed

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

    def _exec_task_done(
        self,
        task: asyncio.Task[ExecResult],
        key: tuple[str, str] | None,
    ) -> None:
        self.exec_tasks.discard(task)
        if key is not None:
            current = self.inflight_execs.get(key)
            if current is not None and current[1] is task:
                self.inflight_execs.pop(key, None)
        # A disconnected handler may be the task's last waiter. Retrieving its
        # exception avoids an unobserved-task warning while the durable pending
        # record remains the source of truth for a later retry.
        if not task.cancelled():
            task.exception()

    async def execute_code(self, session_id: str, request: ExecCreate) -> ExecResult:
        """Run an exec as runtime-owned work, independent of its HTTP waiter."""
        session = self.store.get_session(session_id)
        if session.closing or session_id in self.closing_sessions:
            raise SessionClosingError(f"Session '{session_id}' is closing")
        request_hash = self._exec_request_hash(request)
        key = (session_id, request.exec_id) if request.exec_id is not None else None
        if key is not None:
            inflight = self.inflight_execs.get(key)
            if inflight is not None:
                previous_hash, task = inflight
                if previous_hash != request_hash:
                    raise ExecIdConflictError(
                        f"Execution id '{request.exec_id}' is in flight with a different payload"
                    )
                return await asyncio.shield(task)

        task = asyncio.create_task(
            self._execute_code_once(session_id, request, request_hash=request_hash),
            name=f"opensac-exec-{session_id}",
        )
        self.exec_tasks.add(task)
        self._reserve_session_task(session_id, task)
        if key is not None:
            self.inflight_execs[key] = (request_hash, task)
        task.add_done_callback(lambda finished: self._exec_task_done(finished, key))
        # HTTP disconnect/cancellation only drops this waiter. The execution
        # continues to completion and atomically replaces its pending record.
        return await asyncio.shield(task)

    async def _execute_code_once(
        self,
        session_id: str,
        request: ExecCreate,
        *,
        request_hash: str,
    ) -> ExecResult:
        server_started = time.monotonic()
        # One execution at a time per session. The workspace, the program
        # archive and the broker's reference table are all session-scoped, and
        # two programs sharing them concurrently is not a configuration anyone
        # asks for -- but it used to be reachable, and it silently corrupted the
        # archive by letting one program's code be recorded against another's
        # result. The ceiling on total containers is a separate, global gate.
        session_queue_started = time.monotonic()
        async with self.session_locks[session_id]:
            session_queue_seconds = time.monotonic() - session_queue_started
            prepare_started = time.monotonic()
            session = self.store.get_session(session_id)
            session = self.store.touch_session(session_id)
            if request.exec_id is not None:
                previous = self.store.get_exec_record(session, request.exec_id)
                if previous is not None:
                    if previous.request_hash != request_hash:
                        raise ExecIdConflictError(
                            f"Execution id '{request.exec_id}' was already used "
                            "with a different payload"
                        )
                    if previous.status is ExecRecordStatus.PENDING:
                        raise ExecIndeterminateError(
                            f"Execution id '{request.exec_id}' has an indeterminate prior attempt"
                        )
                    if previous.result is None:
                        raise RuntimeError("Completed execution record has no result")
                    return previous.result
                self.store.save_exec_record(
                    session,
                    ExecRecord(
                        exec_id=request.exec_id,
                        request_hash=request_hash,
                        status=ExecRecordStatus.PENDING,
                        result=None,
                        completed_at=None,
                    ),
                )

            state = self.bind_session(session)
            sequence, program_path = self.store.reserve_program(session, request.code)
            # An execution id is always minted, not only when the caller wants
            # the trace back: the per-program capability counts come from the
            # same trace, and it is drained unconditionally below, so nothing
            # accumulates for a caller that never asks for it.
            execution_id = uuid.uuid4().hex
            request_names = {
                "program_filename": f".opensac-program-{sequence:03d}.py",
                "output_filename": f".opensac-output-{sequence:03d}.json",
            }
            prepare_seconds = time.monotonic() - prepare_started
            with self._exec_workspace(session) as workspace:
                result: SandboxResult | None = None
                rejection: str | None = None
                sandbox_queue_seconds = 0.0
                sandbox_execute_seconds = 0.0
                try:
                    async with self.sandbox_gate.slot() as sandbox_queue_seconds:
                        sandbox_started = time.monotonic()
                        result = await self.sandbox.execute(
                            SandboxRequest(
                                code=request.code,
                                workspace=workspace,
                                session_token=session.token,
                                session_id=session.id,
                                execution_id=execution_id,
                                **request_names,
                            )
                        )
                        sandbox_execute_seconds = time.monotonic() - sandbox_started
                except UnsafeCodeError as exc:
                    # A rejection is a normal observation for the control model,
                    # not a transport error: it has to see the reason and
                    # rewrite the program.
                    rejection = f"Rejected by the sandbox code validator: {exc}"
                    sandbox_execute_seconds = time.monotonic() - sandbox_started

                postprocess_started = time.monotonic()
                if result is not None:
                    await state.policy.record_sandbox_seconds(result.duration_seconds)
                trace = self.broker.take_trace(session.token, execution_id)
                artifacts = self.store.artifacts(session, workspace)

            self.store.record_program(
                session,
                ProgramRecord(
                    sequence=sequence,
                    path=str(program_path),
                    code=request.code,
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
            self.store.touch_session(session_id)
            postprocess_seconds = time.monotonic() - postprocess_started
            timings = dict(result.timings) if result is not None else {}
            timings.update(
                {
                    "session_queue_seconds": session_queue_seconds,
                    "prepare_seconds": prepare_seconds,
                    "sandbox_queue_seconds": sandbox_queue_seconds,
                    "sandbox_execute_seconds": sandbox_execute_seconds,
                    "postprocess_seconds": postprocess_seconds,
                    "server_total_seconds": time.monotonic() - server_started,
                }
            )

            response = ExecResult(
                exec_id=request.exec_id,
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
                trace=self._returned_trace(
                    session,
                    trace,
                    include_trace=request.include_trace,
                ),
                timings=timings,
            )
            if request.exec_id is not None:
                self.store.save_exec_record(
                    session,
                    ExecRecord(
                        exec_id=request.exec_id,
                        request_hash=request_hash,
                        status=ExecRecordStatus.COMPLETED,
                        result=response,
                    ),
                )
            return response

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

    async def execute_run(
        self,
        run: Run,
        session: Session,
        *,
        admitted: bool = False,
    ) -> None:
        async with self.session_locks[session.id]:
            session = self.store.get_session(session.id)
            if not admitted and (
                session.closing or session.id in self.closing_sessions
            ):
                run.status = RunStatus.FAILED
                run.error = "Session is closing"
                self.store.save_run(run)
                await self.publish(
                    run.id,
                    "run.failed",
                    {"run_id": run.id, "status": run.status},
                )
                return
            session = self.store.touch_session(session.id)
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
                self.store.touch_session(session.id)
                event_type = (
                    "run.completed" if run.status == RunStatus.COMPLETED else "run.failed"
                )
                await self.publish(
                    run.id,
                    event_type,
                    {"run_id": run.id, "status": run.status},
                )
            finally:
                self.broker.unregister_session(run_token)


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
                "features": ["idempotent_exec"],
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
    async def healthz() -> dict:
        return {
            "status": "ok",
            "sandbox_mode": settings.sandbox_mode,
            "sandbox": runtime.sandbox_gate.snapshot(),
            "inflight_execs": len(runtime.exec_tasks),
        }

    @app.post("/v1/sessions", response_model=PublicSession, dependencies=[Depends(authorize)])
    async def create_session(request: SessionCreate) -> PublicSession:
        unknown = set(request.backends) - {"web", "local"}
        if unknown:
            raise HTTPException(status_code=422, detail=f"Unknown backends: {sorted(unknown)}")
        # One search backend per session. `search.query` resolves to it, so two
        # would leave the broker picking one and the program unable to tell
        # which -- and a session that silently searched half of what it enabled
        # is the quiet kind of wrong. Mixed retrieval, when there is an
        # experiment that wants it, is an explicit parameter and an arm of its
        # own, not a default nobody chose.
        if len(set(request.backends)) != 1:
            raise HTTPException(
                status_code=422,
                detail=(
                    "A session takes exactly one search backend, got "
                    f"{sorted(set(request.backends))}."
                ),
            )
        session = runtime.store.create_session(request)
        return public_session(session)

    @app.get(
        "/v1/sessions/{session_id}",
        response_model=PublicSession,
        dependencies=[Depends(authorize)],
    )
    async def read_session(session_id: str) -> PublicSession:
        session = get_session(session_id)
        if not session.closing:
            session = runtime.store.touch_session(session_id)
        return public_session(session)

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
        session = get_session(session_id)
        if not session.closing:
            session = runtime.store.touch_session(session_id)
        return runtime.store.snapshot_workspace(
            session,
            max_total_bytes=max(max_total_bytes, 0),
            max_file_bytes=max(max_file_bytes, 0),
        )

    @app.delete("/v1/sessions/{session_id}", dependencies=[Depends(authorize)])
    async def delete_session(session_id: str) -> dict[str, str]:
        try:
            await runtime.close_session(session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Session not found") from exc
        except SessionCleanupError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {"status": "deleted"}

    @app.post(
        "/v1/sessions/{session_id}/runs",
        response_model=PublicRun,
        dependencies=[Depends(authorize)],
    )
    async def create_run(session_id: str, request: RunCreate) -> PublicRun:
        session = get_session(session_id)
        if session.closing:
            raise HTTPException(status_code=409, detail="Session is closing")
        session = runtime.store.touch_session(session_id)
        run = runtime.store.create_run(session_id, request)
        try:
            runtime.start_run_task(run, session)
        except SessionClosingError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
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
        try:
            return await runtime.execute_code(session_id, request)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Session not found") from exc
        except SessionClosingError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ExecIdConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ExecIndeterminateError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

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
