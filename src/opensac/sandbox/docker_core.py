from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from opensac.sandbox.base import SandboxRequest

_OUTPUT_LIMIT_MARKER = (
    b"\nOpenSAC terminated the sandbox process after stdout/stderr reached the output limit.\n"
)
_EXECUTION_WARNING_BYTES = 4_096
_EXECUTION_ENVELOPE_OVERHEAD_BYTES = 64 * 1_024
SANDBOX_CONTRACT = 13
SANDBOX_CONTRACT_LABEL = "org.opensac.sandbox.contract"


class SandboxImageContractError(RuntimeError):
    pass


def broker_socket_mount_args(
    broker_socket: Path,
    *,
    platform: str = sys.platform,
) -> list[str]:
    """Build a broker socket mount compatible with the current Docker host."""

    source = broker_socket.resolve()
    destination = "/run/opensac/broker.sock"
    if platform == "darwin":
        # Docker Desktop's host-socket forwarder handles --volume/-v, while
        # --mount is rewritten to an unavailable /socket_mnt source. The
        # forwarded socket is root:root 0660 regardless of its host ownership,
        # so keep the sandbox user non-root but allow it to connect via GID 0.
        return [
            "--volume",
            f"{source}:{destination}:ro",
            "--group-add",
            "0",
        ]
    return [
        "--mount",
        f"type=bind,src={source},dst={destination},readonly",
    ]


class DockerImageContractVerifier:
    """Lazily reject sandbox images built for an incompatible SDK contract."""

    def __init__(self, image: str, *, expected: int = SANDBOX_CONTRACT) -> None:
        self.image = image
        self.expected = expected
        self._verified = False
        self._lock = asyncio.Lock()

    async def _inspect(self) -> tuple[int, bytes, bytes]:
        try:
            process = await asyncio.create_subprocess_exec(
                "docker",
                "image",
                "inspect",
                "--format",
                f'{{{{ index .Config.Labels "{SANDBOX_CONTRACT_LABEL}" }}}}',
                self.image,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise SandboxImageContractError(
                f"Could not inspect sandbox image {self.image!r}: {type(exc).__name__}: {exc}"
            ) from exc
        stdout, stderr = await process.communicate()
        return process.returncode or 0, stdout, stderr

    async def _pull(self) -> None:
        try:
            process = await asyncio.create_subprocess_exec(
                "docker",
                "pull",
                self.image,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise SandboxImageContractError(
                f"Could not pull sandbox image {self.image!r}: {type(exc).__name__}: {exc}"
            ) from exc
        _, stderr_bytes = await process.communicate()
        if process.returncode != 0:
            stderr = stderr_bytes.decode("utf-8", errors="replace").strip()
            detail = stderr or f"docker pull exited {process.returncode}"
            raise SandboxImageContractError(
                f"Could not pull sandbox image {self.image!r}: {detail}"
            )

    @staticmethod
    def _image_is_missing(stderr_bytes: bytes) -> bool:
        stderr = stderr_bytes.decode("utf-8", errors="replace").lower()
        return "no such image" in stderr or "no such object" in stderr

    async def ensure_compatible(self) -> None:
        if self._verified:
            return
        async with self._lock:
            if self._verified:
                return
            returncode, stdout_bytes, stderr_bytes = await self._inspect()
            if returncode != 0 and self._image_is_missing(stderr_bytes):
                await self._pull()
                returncode, stdout_bytes, stderr_bytes = await self._inspect()
            if returncode != 0:
                stderr = stderr_bytes.decode("utf-8", errors="replace").strip()
                detail = stderr or f"docker image inspect exited {returncode}"
                raise SandboxImageContractError(
                    f"Could not inspect sandbox image {self.image!r}: {detail}"
                )
            actual = stdout_bytes.decode("utf-8", errors="replace").strip()
            if actual != str(self.expected):
                rendered = "missing" if not actual or actual == "<no value>" else actual
                raise SandboxImageContractError(
                    f"Sandbox image {self.image!r} has contract {rendered!r}; "
                    f"expected {self.expected}. Rebuild it with `opensac build-sandbox`."
                )
            self._verified = True


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

    async def read_stream(stream: asyncio.StreamReader | None, destination: bytearray) -> None:
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
            f"Could not remove sandbox container {container_id}: {type(exc).__name__}: {exc}"
        ) from exc
    _, stderr_bytes = await cleanup.communicate()
    stderr = stderr_bytes.decode("utf-8", errors="replace").strip()
    if cleanup.returncode != 0 and "No such container" not in stderr:
        detail = stderr or f"docker rm exited {cleanup.returncode}"
        raise RuntimeError(f"Could not remove sandbox container {container_id}: {detail}")


@dataclass(frozen=True)
class ExecutionWorkspace:
    program_path: Path
    output_path: Path
    ready_path: Path

    @classmethod
    def prepare(cls, request: SandboxRequest) -> ExecutionWorkspace:
        request.workspace.mkdir(parents=True, exist_ok=True)
        workspace = cls(
            program_path=request.workspace / request.program_filename,
            output_path=request.workspace / request.output_filename,
            ready_path=request.workspace / f"{request.output_filename}.ready",
        )
        workspace.program_path.write_text(request.code, encoding="utf-8")
        workspace.output_path.unlink(missing_ok=True)
        workspace.ready_path.unlink(missing_ok=True)
        return workspace

    def read_output(self, *, max_output_bytes: int) -> tuple[dict[str, Any], str]:
        if not self.output_path.exists():
            return {}, ""
        if self.output_path.stat().st_size > max_output_bytes + _EXECUTION_ENVELOPE_OVERHEAD_BYTES:
            return {}, ""
        try:
            value = json.loads(self.output_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}, "\nOpenSAC output was not valid JSON."
        if not isinstance(value, dict):
            return {}, "\nOpenSAC output was not a JSON object."
        warnings = value.get("warnings")
        warning_bytes = len(
            json.dumps(warnings, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        if warning_bytes > _EXECUTION_WARNING_BYTES:
            value.pop("warnings", None)
        submitted = dict(value)
        submitted.pop("warnings", None)
        submitted_bytes = len(
            json.dumps(submitted, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        if submitted_bytes > max_output_bytes:
            return ({"warnings": value.get("warnings", [])} if "warnings" in value else {}), ""
        return value, ""

    def cleanup(self) -> None:
        self.program_path.unlink(missing_ok=True)
        self.output_path.unlink(missing_ok=True)
        self.ready_path.unlink(missing_ok=True)


class DockerSandboxCore:
    """Shared Docker policy and execution-file mechanics for sandbox strategies."""

    def __init__(
        self,
        *,
        image: str,
        broker_socket: Path,
        docker_host_platform: str = sys.platform,
        timeout_seconds: int = 120,
        memory: str = "512m",
        cpus: float = 1.0,
        pids_limit: int = 64,
        max_output_bytes: int = 1_000_000,
    ) -> None:
        self.image = image
        self.broker_socket = broker_socket.resolve()
        self.docker_host_platform = docker_host_platform
        self.timeout_seconds = timeout_seconds
        self.memory = memory
        self.cpus = cpus
        self.pids_limit = pids_limit
        self.max_output_bytes = max_output_bytes
        self._image_contract = DockerImageContractVerifier(image)

    def _docker_run_command(
        self,
        request: SandboxRequest,
        *,
        cid_path: Path,
        detach: bool = False,
        init: bool = False,
        extra_args: tuple[str, ...] = (),
    ) -> list[str]:
        command = ["docker", "run"]
        if detach:
            command.append("--detach")
        command.append("--rm")
        if init:
            command.append("--init")
        command += [
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
        # Hosts without a mounted cpu cgroup controller reject --cpus outright.
        if self.cpus > 0:
            command += ["--cpus", str(self.cpus)]
        command += [
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=64m",
            "--cidfile",
            str(cid_path),
            *extra_args,
            "--mount",
            f"type=bind,src={self.host_mount_workspace(request)},dst=/workspace",
        ]
        command += broker_socket_mount_args(
            self.broker_socket,
            platform=self.docker_host_platform,
        )
        command += [
            "--env",
            "OPENSAC_BROKER_SOCKET=/run/opensac/broker.sock",
            "--env",
            "OPENSAC_WORKSPACE=/workspace",
        ]
        return command

    @staticmethod
    def host_mount_workspace(request: SandboxRequest) -> Path:
        return (request.mount_workspace or request.workspace).resolve()

    @classmethod
    def container_execution_workspace(cls, request: SandboxRequest) -> str:
        mount = cls.host_mount_workspace(request)
        workspace = request.workspace.resolve()
        try:
            relative = workspace.relative_to(mount)
        except ValueError as exc:
            raise ValueError("execution workspace must be inside the sandbox mount") from exc
        return str(PurePosixPath("/workspace", *relative.parts))

    def _execution_timeout(self, request: SandboxRequest) -> float:
        return min(
            self.timeout_seconds,
            request.timeout_seconds
            if request.timeout_seconds is not None
            else self.timeout_seconds,
        )

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
        """Distinguish a Docker refusal from a program exiting with code 125."""

        if returncode != 125:
            return None
        first_line = stderr.strip().splitlines()[0] if stderr.strip() else ""
        if not first_line.startswith("docker:"):
            return None
        return f"The sandbox container could not be started: {first_line}"
