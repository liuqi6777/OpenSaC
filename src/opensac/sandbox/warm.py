from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from opensac.sandbox.base import SandboxRequest, SandboxResult
from opensac.sandbox.docker import (
    DockerImageContractVerifier,
    DockerSandbox,
    SandboxImageContractError,
    read_bounded_process_output,
    remove_docker_container,
)
from opensac.sandbox.validator import validate_code


class SessionLike(Protocol):
    """The lifecycle fields needed by :meth:`WarmDockerSandbox.close_session`."""

    id: str
    token: str
    workspace: str


@dataclass
class _WarmSession:
    key: str
    workspace: Path
    last_used: float
    container_id: str | None = None
    baseline_pids: frozenset[int] | None = None
    poisoned: bool = False
    starting: bool = False
    leases: int = 0
    closing: bool = False
    execute_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    no_leases: asyncio.Event = field(default_factory=asyncio.Event)
    closed: asyncio.Event = field(default_factory=asyncio.Event)
    close_task: asyncio.Task[None] | None = None

    def __post_init__(self) -> None:
        self.no_leases.set()


class _StartFailure(Exception):
    def __init__(self, exit_code: int, stdout: str, stderr: str, launch_error: str) -> None:
        super().__init__(launch_error)
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.launch_error = launch_error


class WarmDockerSandbox:
    """One lazily-created, resource-limited Docker container per session.

    A container keeps only the namespace and mounts warm.  Every ``execute``
    still starts a new isolated Python interpreter with ``docker exec``.  The
    broker credential is injected into that child rather than frozen into the
    container because one OpenSAC session may mint a different token per run.

    Executions for one session are serialized inside this class.  Different
    sessions remain independent, and lifecycle operations wait for a current
    execution before removing its container.  A timeout or cancellation drops
    the entire container so a child process cannot survive into the next turn.
    """

    _KEEPALIVE = "import time; time.sleep(315360000)"
    _ENTRYPOINT = "/opt/runner/entrypoint.py"

    def __init__(
        self,
        *,
        image: str,
        broker_socket: Path,
        timeout_seconds: int = 120,
        memory: str = "512m",
        cpus: float = 1.0,
        pids_limit: int = 64,
        max_output_bytes: int = 1_000_000,
        startup_timeout_seconds: float = 60.0,
        idle_timeout_seconds: float = 300.0,
        max_containers: int = 0,
    ) -> None:
        self.image = image
        self.broker_socket = broker_socket.resolve()
        self.timeout_seconds = timeout_seconds
        self.memory = memory
        self.cpus = cpus
        self.pids_limit = pids_limit
        self.max_output_bytes = max_output_bytes
        self.startup_timeout_seconds = startup_timeout_seconds
        self.idle_timeout_seconds = idle_timeout_seconds
        self.max_containers = max(0, int(max_containers))
        self._image_contract = DockerImageContractVerifier(image)
        self._sessions: dict[str, _WarmSession] = {}
        self._closed_session_keys: set[str] = set()
        self._registry_lock = asyncio.Lock()
        self._closing_tasks: set[asyncio.Task[None]] = set()
        self._closed = False
        self._capacity_changed = asyncio.Event()
        self._capacity_changed.set()
        self._capacity_waiting = 0

    def snapshot(self) -> dict[str, int]:
        containers = sum(
            state.container_id is not None for state in self._sessions.values()
        )
        return {
            "capacity": self.max_containers,
            "active": containers,
            "waiting": self._capacity_waiting,
            "limit": self.max_containers,
            "containers": containers,
            "starting": sum(state.starting for state in self._sessions.values()),
            "sessions": len(self._sessions),
            "busy": sum(state.leases > 0 for state in self._sessions.values()),
        }

    @property
    def owner_label(self) -> str:
        return hashlib.sha256(str(self.broker_socket).encode()).hexdigest()[:16]

    @staticmethod
    def _session_key(request: SandboxRequest) -> str:
        return request.session_id or request.session_token

    def container_command(
        self,
        request: SandboxRequest,
        *,
        cid_path: Path | None = None,
    ) -> list[str]:
        """Build the long-lived container command without embedding credentials."""

        workspace = request.workspace.resolve()
        cid_path = cid_path or self.broker_socket.parent / "containers" / "warm-test.cid"
        session_digest = hashlib.sha256(self._session_key(request).encode()).hexdigest()[:16]
        command = [
            "docker",
            "run",
            "--detach",
            "--rm",
            "--init",
            "--network",
            "none",
            "--read-only",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            str(self.pids_limit),
            "--memory",
            self.memory,
        ]
        if self.cpus > 0:
            command += ["--cpus", str(self.cpus)]
        command += [
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=64m",
            "--cidfile",
            str(cid_path),
            "--label",
            "opensac.sandbox=warm",
            "--label",
            f"opensac.owner={self.owner_label}",
            "--label",
            f"opensac.session={session_digest}",
            "--mount",
            f"type=bind,src={workspace},dst=/workspace",
            "--mount",
            f"type=bind,src={self.broker_socket},dst=/run/opensac/broker.sock,readonly",
            "--env",
            "OPENSAC_BROKER_SOCKET=/run/opensac/broker.sock",
            "--env",
            "OPENSAC_WORKSPACE=/workspace",
            "--workdir",
            "/workspace",
            "--entrypoint",
            "python",
            self.image,
            "-I",
            "-c",
            self._KEEPALIVE,
        ]
        return command

    async def reap_orphans(self) -> int:
        """Remove containers left by a crashed process owning this broker path."""

        try:
            process = await asyncio.create_subprocess_exec(
                "docker",
                "ps",
                "--all",
                "--quiet",
                "--no-trunc",
                "--filter",
                "label=opensac.sandbox=warm",
                "--filter",
                f"label=opensac.owner={self.owner_label}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise RuntimeError(
                f"Could not enumerate orphan warm containers: {type(exc).__name__}: {exc}"
            ) from exc
        stdout, stderr_bytes = await process.communicate()
        if process.returncode != 0:
            stderr = stderr_bytes.decode("utf-8", errors="replace").strip()
            detail = stderr or f"docker ps exited {process.returncode}"
            raise RuntimeError(f"Could not enumerate orphan warm containers: {detail}")
        tracked = {
            state.container_id
            for state in self._sessions.values()
            if state.container_id is not None
        }
        orphan_ids = [
            container_id
            for container_id in stdout.decode("utf-8", errors="replace").split()
            if container_id not in tracked
        ]
        await asyncio.gather(*(self._remove_container(item) for item in orphan_ids))
        return len(orphan_ids)

    async def _container_pids(self, container_id: str) -> frozenset[int]:
        """Return a host-observed process snapshot for one warm container."""

        try:
            process = await asyncio.create_subprocess_exec(
                "docker",
                "top",
                container_id,
                "-eo",
                "pid",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise RuntimeError(
                f"Could not audit warm container {container_id}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        stdout_bytes, stderr_bytes = await process.communicate()
        stderr = stderr_bytes.decode("utf-8", errors="replace").strip()
        if process.returncode != 0:
            detail = stderr or f"docker top exited {process.returncode}"
            raise RuntimeError(f"Could not audit warm container {container_id}: {detail}")
        lines = stdout_bytes.decode("utf-8", errors="replace").splitlines()
        if not lines or lines[0].strip().upper() != "PID":
            raise RuntimeError(f"Could not audit warm container {container_id}: invalid header")
        try:
            pids = frozenset(int(line.strip()) for line in lines[1:] if line.strip())
        except ValueError as exc:
            raise RuntimeError(
                f"Could not audit warm container {container_id}: invalid PID output"
            ) from exc
        if not pids or any(pid <= 0 for pid in pids):
            raise RuntimeError(f"Could not audit warm container {container_id}: no processes")
        return pids

    async def _untracked_session_containers(self, key: str) -> set[str]:
        """Find containers after a process restart left the registry empty."""

        session_digest = hashlib.sha256(key.encode()).hexdigest()[:16]
        try:
            process = await asyncio.create_subprocess_exec(
                "docker",
                "ps",
                "--all",
                "--quiet",
                "--no-trunc",
                "--filter",
                "label=opensac.sandbox=warm",
                "--filter",
                f"label=opensac.owner={self.owner_label}",
                "--filter",
                f"label=opensac.session={session_digest}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise RuntimeError(
                f"Could not enumerate warm containers for session {key!r}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        stdout_bytes, stderr_bytes = await process.communicate()
        stderr = stderr_bytes.decode("utf-8", errors="replace").strip()
        if process.returncode != 0:
            detail = stderr or f"docker ps exited {process.returncode}"
            raise RuntimeError(
                f"Could not enumerate warm containers for session {key!r}: {detail}"
            )
        return set(stdout_bytes.decode("utf-8", errors="replace").split())

    def execution_command(self, container_id: str, request: SandboxRequest) -> list[str]:
        command = [
            "docker",
            "exec",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--workdir",
            "/workspace",
            "--env",
            f"OPENSAC_SESSION_TOKEN={request.session_token}",
            "--env",
            f"OPENSAC_OUTPUT_PATH=/workspace/{request.output_filename}",
            "--env",
            f"OPENSAC_READY_PATH=/workspace/{request.output_filename}.ready",
        ]
        if request.execution_id:
            command += ["--env", f"OPENSAC_EXECUTION_ID={request.execution_id}"]
        command += [
            container_id,
            "python",
            "-I",
            self._ENTRYPOINT,
            f"/workspace/{request.program_filename}",
        ]
        return command

    @asynccontextmanager
    async def _lease(self, request: SandboxRequest) -> AsyncIterator[_WarmSession]:
        state = await self._reserve(request)
        try:
            await state.execute_lock.acquire()
        except BaseException:
            await self._release(state, used=False)
            raise
        try:
            yield state
        finally:
            state.execute_lock.release()
            await self._release(state, used=True)

    async def _reserve(self, request: SandboxRequest) -> _WarmSession:
        key = self._session_key(request)
        workspace = request.workspace.resolve()
        while True:
            waiter: asyncio.Event | None = None
            replace: _WarmSession | None = None
            async with self._registry_lock:
                if self._closed:
                    raise RuntimeError("WarmDockerSandbox is closed")
                if key in self._closed_session_keys:
                    raise RuntimeError(f"Warm sandbox session {key!r} is closed")
                state = self._sessions.get(key)
                if state is None:
                    state = _WarmSession(key=key, workspace=workspace, last_used=time.monotonic())
                    self._sessions[key] = state
                if state.closing:
                    waiter = state.closed
                elif state.workspace != workspace:
                    replace = state
                else:
                    state.leases += 1
                    state.no_leases.clear()
                    return state
            if waiter is not None:
                await waiter.wait()
            elif replace is not None:
                await self._close_key(key, expected=replace)

    async def _release(self, state: _WarmSession, *, used: bool) -> None:
        async with self._registry_lock:
            if used:
                state.last_used = time.monotonic()
            state.leases -= 1
            if state.leases == 0:
                state.no_leases.set()
                self._capacity_changed.set()

    async def _start_container(self, state: _WarmSession, request: SandboxRequest) -> str:
        try:
            await self._image_contract.ensure_compatible()
        except SandboxImageContractError as exc:
            raise _StartFailure(125, "", str(exc), str(exc)) from exc
        await self._ensure_container_capacity(state)
        containers_dir = self.broker_socket.parent / "containers"
        containers_dir.mkdir(parents=True, exist_ok=True)
        cid_path = containers_dir / f"warm-{uuid.uuid4().hex}.cid"
        cid_path.unlink(missing_ok=True)
        try:
            try:
                process = await asyncio.create_subprocess_exec(
                    *self.container_command(request, cid_path=cid_path),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except OSError as exc:
                message = f"docker: {type(exc).__name__}: {exc}"
                raise _StartFailure(
                    125,
                    "",
                    message,
                    f"The sandbox container could not be started: {message}",
                ) from exc
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    process.communicate(), timeout=self.startup_timeout_seconds
                )
            except TimeoutError as exc:
                process.kill()
                stdout_bytes, stderr_bytes = await process.communicate()
                container_id = self._read_cid(cid_path)
                if container_id:
                    await self._remove_container(container_id)
                stderr = stderr_bytes.decode("utf-8", errors="replace")
                message = stderr.strip() or "docker run timed out"
                raise _StartFailure(
                    125,
                    stdout_bytes.decode("utf-8", errors="replace"),
                    stderr,
                    f"The sandbox container could not be started: {message}",
                ) from exc
            except asyncio.CancelledError:
                process.kill()
                await process.communicate()
                container_id = self._read_cid(cid_path)
                if container_id:
                    await self._remove_container(container_id)
                raise

            stdout = stdout_bytes.decode("utf-8", errors="replace")
            stderr = stderr_bytes.decode("utf-8", errors="replace")
            stdout_lines = stdout.strip().splitlines()
            container_id = self._read_cid(cid_path) or (stdout_lines[0] if stdout_lines else "")
            if process.returncode != 0 or not container_id:
                if container_id:
                    await self._remove_container(container_id)
                first_line = (
                    stderr.strip().splitlines()[0] if stderr.strip() else "docker run failed"
                )
                launch_error = DockerSandbox._launch_error(process.returncode, stderr)
                raise _StartFailure(
                    process.returncode if process.returncode is not None else 125,
                    stdout,
                    stderr,
                    launch_error
                    or f"The sandbox container could not be started: {first_line}",
                )
            state.container_id = container_id
            try:
                state.baseline_pids = await self._container_pids(container_id)
            except Exception as exc:
                await self._discard_container(state)
                raise _StartFailure(
                    125,
                    stdout,
                    stderr,
                    f"The sandbox container could not be started: {exc}",
                ) from exc
            return container_id
        finally:
            cid_path.unlink(missing_ok=True)
            async with self._registry_lock:
                state.starting = False
                self._capacity_changed.set()

    async def _ensure_container_capacity(self, current: _WarmSession) -> None:
        """Bound warm namespaces, evicting only an idle least-recently-used one."""

        if self.max_containers <= 0:
            return
        while True:
            close_task: asyncio.Task[None] | None = None
            waiter: asyncio.Event | None = None
            async with self._registry_lock:
                occupied = [
                    state
                    for state in self._sessions.values()
                    if state.container_id is not None or state.starting
                ]
                if len(occupied) < self.max_containers:
                    current.starting = True
                    return
                candidates = [
                    state
                    for state in occupied
                    if state is not current
                    and state.container_id is not None
                    and not state.starting
                    and not state.closing
                    and state.leases == 0
                ]
                if candidates:
                    victim = min(candidates, key=lambda state: state.last_used)
                    close_task = self._start_close_locked(victim.key, victim)
                else:
                    self._capacity_changed.clear()
                    self._capacity_waiting += 1
                    waiter = self._capacity_changed
            if close_task is not None:
                await asyncio.shield(close_task)
            elif waiter is not None:
                try:
                    await waiter.wait()
                finally:
                    self._capacity_waiting -= 1

    @staticmethod
    def _read_cid(cid_path: Path) -> str:
        if not cid_path.exists():
            return ""
        return cid_path.read_text(encoding="utf-8").strip()

    async def _ensure_container(
        self, state: _WarmSession, request: SandboxRequest
    ) -> str:
        if state.poisoned:
            await self._discard_container(state)
        if state.container_id:
            return state.container_id
        return await self._start_container(state, request)

    async def execute(self, request: SandboxRequest) -> SandboxResult:
        validation_started = time.monotonic()
        validate_code(request.code)
        validation_seconds = time.monotonic() - validation_started
        setup_started = time.monotonic()
        request.workspace.mkdir(parents=True, exist_ok=True)
        program_path = request.workspace / request.program_filename
        output_path = request.workspace / request.output_filename
        ready_path = request.workspace / f"{request.output_filename}.ready"
        program_path.write_text(request.code, encoding="utf-8")
        output_path.unlink(missing_ok=True)
        ready_path.unlink(missing_ok=True)
        workspace_setup_seconds = time.monotonic() - setup_started
        started = time.monotonic()
        try:
            async with self._lease(request) as state:
                container_was_running = state.container_id is not None and not state.poisoned
                container_started = time.monotonic()
                try:
                    container_id = await self._ensure_container(state, request)
                except _StartFailure as exc:
                    duration_seconds = time.monotonic() - started
                    return SandboxResult(
                        exit_code=exc.exit_code,
                        stdout=exc.stdout[: self.max_output_bytes],
                        stderr=exc.stderr[: self.max_output_bytes],
                        duration_seconds=duration_seconds,
                        timings=self._timings(
                            validation_seconds=validation_seconds,
                            workspace_setup_seconds=workspace_setup_seconds,
                            container_start_seconds=duration_seconds,
                            process_start_seconds=0.0,
                            process_duration_seconds=0.0,
                            duration_seconds=duration_seconds,
                        ),
                        launch_error=exc.launch_error,
                    )
                container_start_seconds = (
                    0.0 if container_was_running else time.monotonic() - container_started
                )

                process_started = time.monotonic()
                process_wall_started = time.time()
                try:
                    process = await asyncio.create_subprocess_exec(
                        *self.execution_command(container_id, request),
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                except OSError as exc:
                    message = f"docker: {type(exc).__name__}: {exc}"
                    duration_seconds = time.monotonic() - started
                    return SandboxResult(
                        exit_code=125,
                        stdout="",
                        stderr=message,
                        duration_seconds=duration_seconds,
                        timings=self._timings(
                            validation_seconds=validation_seconds,
                            workspace_setup_seconds=workspace_setup_seconds,
                            container_start_seconds=container_start_seconds,
                            process_start_seconds=time.monotonic() - process_started,
                            process_duration_seconds=time.monotonic() - process_started,
                            duration_seconds=duration_seconds,
                        ),
                        launch_error=f"The sandbox process could not be started: {message}",
                    )

                try:
                    captured = await read_bounded_process_output(
                        process,
                        max_output_bytes=self.max_output_bytes,
                        timeout_seconds=min(
                            self.timeout_seconds,
                            request.timeout_seconds
                            if request.timeout_seconds is not None
                            else self.timeout_seconds,
                        ),
                    )
                except asyncio.CancelledError:
                    await self._discard_container(state)
                    raise
                if captured.timed_out or captured.output_limit_exceeded:
                    await self._discard_container(state)

                process_duration_seconds = time.monotonic() - process_started
                stdout = captured.stdout.decode("utf-8", errors="replace")
                stderr = captured.stderr.decode("utf-8", errors="replace")
                submitted = {}
                if output_path.exists() and output_path.stat().st_size <= self.max_output_bytes:
                    try:
                        submitted = json.loads(output_path.read_text(encoding="utf-8"))
                    except json.JSONDecodeError:
                        stderr += "\nOpenSAC output was not valid JSON."
                launch_error = self._execution_launch_error(process.returncode, stderr)
                if launch_error:
                    await self._discard_container(state)
                elif not captured.timed_out and not captured.output_limit_exceeded:
                    try:
                        current_pids = await self._container_pids(container_id)
                        if current_pids != state.baseline_pids:
                            raise RuntimeError(
                                "warm container process set changed after execution"
                            )
                    except Exception as exc:
                        await self._discard_container(state)
                        launch_error = f"The warm sandbox process audit failed: {exc}"
                duration_seconds = time.monotonic() - started
                process_start_seconds = DockerSandbox._startup_seconds(
                    ready_path,
                    wall_started=process_wall_started,
                    duration_seconds=process_duration_seconds,
                )
                return SandboxResult(
                    exit_code=process.returncode if process.returncode is not None else -1,
                    stdout=stdout,
                    stderr=stderr,
                    duration_seconds=duration_seconds,
                    output=submitted.get("output"),
                    citations=submitted.get("citations", []),
                    timed_out=captured.timed_out,
                    timings=self._timings(
                        validation_seconds=validation_seconds,
                        workspace_setup_seconds=workspace_setup_seconds,
                        container_start_seconds=container_start_seconds,
                        process_start_seconds=process_start_seconds,
                        process_duration_seconds=process_duration_seconds,
                        duration_seconds=duration_seconds,
                    ),
                    launch_error=launch_error,
                )
        finally:
            program_path.unlink(missing_ok=True)
            output_path.unlink(missing_ok=True)
            ready_path.unlink(missing_ok=True)

    @staticmethod
    def _timings(
        *,
        validation_seconds: float,
        workspace_setup_seconds: float,
        container_start_seconds: float,
        process_start_seconds: float,
        process_duration_seconds: float,
        duration_seconds: float,
    ) -> dict[str, float]:
        startup_seconds = min(
            container_start_seconds + process_start_seconds,
            duration_seconds,
        )
        return {
            "validation_seconds": validation_seconds,
            "workspace_setup_seconds": workspace_setup_seconds,
            "container_start_seconds": container_start_seconds,
            "process_start_seconds": process_start_seconds,
            "startup_seconds": startup_seconds,
            "program_seconds": max(
                process_duration_seconds - process_start_seconds, 0.0
            ),
            "result_processing_seconds": max(
                duration_seconds
                - container_start_seconds
                - process_duration_seconds,
                0.0,
            ),
            "sandbox_total_seconds": duration_seconds,
        }

    async def _discard_container(self, state: _WarmSession) -> None:
        container_id = state.container_id
        if not container_id:
            state.baseline_pids = None
            state.poisoned = False
            return
        state.poisoned = True
        await self._remove_container(container_id)
        state.container_id = None
        state.baseline_pids = None
        state.poisoned = False
        self._capacity_changed.set()

    @staticmethod
    def _execution_launch_error(returncode: int | None, stderr: str) -> str | None:
        launch_error = DockerSandbox._launch_error(returncode, stderr)
        if launch_error:
            return launch_error
        first_line = stderr.strip().splitlines()[0] if stderr.strip() else ""
        if first_line.startswith("Error response from daemon:"):
            return f"The sandbox process could not be started: {first_line}"
        return None

    @staticmethod
    async def _remove_container(container_id: str) -> None:
        await remove_docker_container(container_id)

    async def _finish_close(self, key: str, state: _WarmSession) -> None:
        try:
            await state.no_leases.wait()
            async with state.execute_lock:
                await self._discard_container(state)
        except BaseException:
            async with self._registry_lock:
                if self._sessions.get(key) is state:
                    state.closing = False
                state.closed.set()
            raise
        else:
            async with self._registry_lock:
                if self._sessions.get(key) is state:
                    self._sessions.pop(key, None)
                state.closed.set()
                self._capacity_changed.set()

    def _start_close_locked(
        self, key: str, state: _WarmSession
    ) -> asyncio.Task[None]:
        state.closing = True
        state.closed = asyncio.Event()
        task = asyncio.create_task(self._finish_close(key, state))
        state.close_task = task
        self._closing_tasks.add(task)
        task.add_done_callback(self._closing_tasks.discard)
        return task

    async def _close_key(self, key: str, *, expected: _WarmSession | None = None) -> bool:
        async with self._registry_lock:
            state = self._sessions.get(key)
            if state is None or (expected is not None and state is not expected):
                return False
            if state.closing:
                task = state.close_task
                if task is None:
                    raise RuntimeError(f"Warm sandbox session {key!r} has no close task")
            else:
                task = self._start_close_locked(key, state)
        await asyncio.shield(task)
        return True

    async def close_session(self, session: SessionLike) -> None:
        """Remove the warm container for a duck-typed OpenSAC session."""

        keys = [str(session.id)]
        token = str(session.token)
        if token not in keys:
            # Also closes containers made by an older caller that did not yet
            # populate SandboxRequest.session_id.
            keys.append(token)
        # A close is permanent for this session id. Mark it before waiting for
        # an in-flight execution so a concurrent late caller cannot recreate a
        # container in the gap between removal and this method returning.
        async with self._registry_lock:
            self._closed_session_keys.update(keys)
        await asyncio.gather(*(self._close_key(key) for key in keys))
        orphan_sets = await asyncio.gather(
            *(self._untracked_session_containers(key) for key in keys)
        )
        orphan_ids = set().union(*orphan_sets)
        await asyncio.gather(
            *(self._remove_container(container_id) for container_id in orphan_ids)
        )

    async def reap_idle(self, max_idle_seconds: float | None = None) -> int:
        """Close currently idle session containers and return how many were claimed."""

        idle_seconds = self.idle_timeout_seconds if max_idle_seconds is None else max_idle_seconds
        if idle_seconds < 0:
            raise ValueError("max_idle_seconds must be non-negative")
        cutoff = time.monotonic() - idle_seconds
        claimed: list[asyncio.Task[None]] = []
        async with self._registry_lock:
            for key, state in self._sessions.items():
                if state.closing or state.leases or state.last_used > cutoff:
                    continue
                claimed.append(self._start_close_locked(key, state))
        await asyncio.gather(*(asyncio.shield(task) for task in claimed))
        return len(claimed)

    async def close(self) -> None:
        """Stop accepting executions and remove every warm container."""

        async with self._registry_lock:
            self._closed = True
            keys = list(self._sessions)
        await asyncio.gather(*(self._close_key(key) for key in keys))
