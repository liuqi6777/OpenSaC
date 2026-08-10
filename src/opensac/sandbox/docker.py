from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from opensac.sandbox.base import SandboxRequest, SandboxResult
from opensac.sandbox.validator import validate_code

_OUTPUT_LIMIT_MARKER = (
    b"\nOpenSAC terminated the sandbox process after stdout/stderr reached "
    b"the output limit.\n"
)


@dataclass(frozen=True)
class BoundedProcessOutput:
    stdout: bytes
    stderr: bytes
    timed_out: bool
    output_limit_exceeded: bool


async def read_bounded_process_output(
    process: asyncio.subprocess.Process,
    *,
    max_output_bytes: int,
    timeout_seconds: float,
) -> BoundedProcessOutput:
    """Drain both pipes concurrently without retaining unbounded child output."""

    stdout = bytearray()
    stderr = bytearray()
    retained = 0
    output_limit = asyncio.Event()
    capture = True

    async def read_stream(
        stream: asyncio.StreamReader | None, destination: bytearray
    ) -> None:
        nonlocal capture, retained
        if stream is None:
            return
        while chunk := await stream.read(64 * 1024):
            if not capture:
                continue
            remaining = max(max_output_bytes - retained, 0)
            if remaining:
                kept = chunk[:remaining]
                destination.extend(kept)
                retained += len(kept)
            if len(chunk) > remaining:
                capture = False
                output_limit.set()

    readers = [read_stream(process.stdout, stdout), read_stream(process.stderr, stderr)]

    async def collect() -> None:
        await asyncio.gather(process.wait(), *readers)

    collector = asyncio.create_task(collect())
    limit_waiter = asyncio.create_task(output_limit.wait())
    timed_out = False
    try:
        done, _ = await asyncio.wait(
            {collector, limit_waiter},
            timeout=timeout_seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            timed_out = True
            capture = False
        if timed_out or output_limit.is_set():
            with suppress(ProcessLookupError):
                process.kill()
        await asyncio.shield(collector)
    except asyncio.CancelledError:
        capture = False
        with suppress(ProcessLookupError):
            process.kill()
        await asyncio.shield(collector)
        raise
    finally:
        limit_waiter.cancel()
        await asyncio.gather(limit_waiter, return_exceptions=True)

    output_limit_exceeded = output_limit.is_set()
    if output_limit_exceeded:
        stderr.extend(_OUTPUT_LIMIT_MARKER)
    return BoundedProcessOutput(
        stdout=bytes(stdout),
        stderr=bytes(stderr),
        timed_out=timed_out,
        output_limit_exceeded=output_limit_exceeded,
    )


async def remove_docker_container(container_id: str) -> None:
    """Force-remove a container, treating an already-gone id as success."""

    try:
        cleanup = await asyncio.create_subprocess_exec(
            "docker",
            "rm",
            "--force",
            container_id,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise RuntimeError(
            f"Could not remove sandbox container {container_id}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    _, stderr_bytes = await cleanup.communicate()
    stderr = stderr_bytes.decode("utf-8", errors="replace").strip()
    if cleanup.returncode != 0 and "No such container" not in stderr:
        detail = stderr or f"docker rm exited {cleanup.returncode}"
        raise RuntimeError(f"Could not remove sandbox container {container_id}: {detail}")


class DockerSandbox:
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
    ) -> None:
        self.image = image
        self.broker_socket = broker_socket.resolve()
        self.timeout_seconds = timeout_seconds
        self.memory = memory
        self.cpus = cpus
        self.pids_limit = pids_limit
        self.max_output_bytes = max_output_bytes

    def command(self, request: SandboxRequest, *, cid_path: Path | None = None) -> list[str]:
        workspace = request.workspace.resolve()
        cid_path = cid_path or self.broker_socket.parent / "containers" / "test.cid"
        command = [
            "docker",
            "run",
            "--rm",
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
        # Hosts without a mounted cpu cgroup controller reject --cpus outright
        # ("NanoCPUs can not be set"), so allow opting out with cpus <= 0.
        if self.cpus > 0:
            command += ["--cpus", str(self.cpus)]
        command += [
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=64m",
            "--cidfile",
            str(cid_path),
            "--mount",
            f"type=bind,src={workspace},dst=/workspace",
            "--mount",
            f"type=bind,src={self.broker_socket},dst=/run/opensac/broker.sock,readonly",
            "--env",
            "OPENSAC_BROKER_SOCKET=/run/opensac/broker.sock",
            "--env",
            f"OPENSAC_SESSION_TOKEN={request.session_token}",
            "--env",
            "OPENSAC_WORKSPACE=/workspace",
            "--env",
            f"OPENSAC_OUTPUT_PATH=/workspace/{request.output_filename}",
            "--env",
            f"OPENSAC_READY_PATH=/workspace/{request.output_filename}.ready",
        ]
        if request.execution_id:
            command += ["--env", f"OPENSAC_EXECUTION_ID={request.execution_id}"]
        command += [self.image, f"/workspace/{request.program_filename}"]
        return command

    async def execute(self, request: SandboxRequest) -> SandboxResult:
        validation_started = time.monotonic()
        validate_code(request.code)
        validation_seconds = time.monotonic() - validation_started
        setup_started = time.monotonic()
        request.workspace.mkdir(parents=True, exist_ok=True)
        program_path = request.workspace / request.program_filename
        output_path = request.workspace / request.output_filename
        ready_path = request.workspace / f"{request.output_filename}.ready"
        containers_dir = self.broker_socket.parent / "containers"
        containers_dir.mkdir(parents=True, exist_ok=True)
        cid_path = containers_dir / f"{uuid.uuid4().hex}.cid"
        program_path.write_text(request.code, encoding="utf-8")
        output_path.unlink(missing_ok=True)
        ready_path.unlink(missing_ok=True)
        cid_path.unlink(missing_ok=True)
        workspace_setup_seconds = time.monotonic() - setup_started
        started = time.monotonic()
        wall_started = time.time()
        process = await asyncio.create_subprocess_exec(
            *self.command(request, cid_path=cid_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            captured = await read_bounded_process_output(
                process,
                max_output_bytes=self.max_output_bytes,
                timeout_seconds=self.timeout_seconds,
            )
        except asyncio.CancelledError:
            await self._remove_container(cid_path)
            raise
        if captured.timed_out or captured.output_limit_exceeded:
            await self._remove_container(cid_path)

        process_duration_seconds = time.monotonic() - started
        stdout = captured.stdout.decode("utf-8", errors="replace")
        stderr = captured.stderr.decode("utf-8", errors="replace")
        submitted = {}
        if output_path.exists() and output_path.stat().st_size <= self.max_output_bytes:
            try:
                submitted = json.loads(output_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                stderr += "\nOpenSAC output was not valid JSON."
        duration_seconds = time.monotonic() - started
        startup_seconds = self._startup_seconds(
            ready_path,
            wall_started=wall_started,
            duration_seconds=duration_seconds,
        )
        result = SandboxResult(
            exit_code=process.returncode if process.returncode is not None else -1,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=duration_seconds,
            output=submitted.get("output"),
            citations=submitted.get("citations", []),
            timed_out=captured.timed_out,
            timings={
                "validation_seconds": validation_seconds,
                "workspace_setup_seconds": workspace_setup_seconds,
                "startup_seconds": startup_seconds,
                "program_seconds": max(
                    process_duration_seconds - startup_seconds, 0.0
                ),
                "result_processing_seconds": max(
                    duration_seconds - process_duration_seconds, 0.0
                ),
                "sandbox_total_seconds": duration_seconds,
            },
            launch_error=self._launch_error(process.returncode, stderr),
        )
        cid_path.unlink(missing_ok=True)
        # Both files are per-execution now, so nothing later overwrites them and
        # they would otherwise pile up in a workspace the program has to keep
        # listing. The archived copy of the program lives outside the workspace.
        program_path.unlink(missing_ok=True)
        output_path.unlink(missing_ok=True)
        ready_path.unlink(missing_ok=True)
        return result

    @staticmethod
    def _startup_seconds(
        ready_path: Path,
        *,
        wall_started: float,
        duration_seconds: float,
    ) -> float:
        if not ready_path.exists():
            return 0.0
        try:
            ready_at = int(ready_path.read_text(encoding="utf-8")) / 1_000_000_000
        except (OSError, ValueError):
            return 0.0
        return min(max(ready_at - wall_started, 0.0), duration_seconds)

    @staticmethod
    def _launch_error(returncode: int | None, stderr: str) -> str | None:
        """Tell "the container never started" apart from "the program failed".

        Docker reserves exit code 125 for its own refusals -- unknown image,
        unreachable daemon, a resource flag the host will not honour -- and
        prefixes those messages with "docker:". Everything else on 125 came
        from the program, which is free to exit with any code it likes.

        Reporting these as ordinary program failures is actively harmful: a
        control model reads the traceback slot, assumes its code is wrong, and
        rewrites it until the turn budget runs out. The resulting transcript
        looks like a model that could not solve the task.
        """
        if returncode != 125:
            return None
        first_line = stderr.strip().splitlines()[0] if stderr.strip() else ""
        if not first_line.startswith("docker:"):
            return None
        return f"The sandbox container could not be started: {first_line}"

    @staticmethod
    async def _remove_container(cid_path: Path) -> None:
        if not cid_path.exists():
            return
        container_id = cid_path.read_text(encoding="utf-8").strip()
        if container_id:
            await remove_docker_container(container_id)
        cid_path.unlink(missing_ok=True)
