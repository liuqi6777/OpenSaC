from __future__ import annotations

import asyncio
import sys
import time
import uuid
from pathlib import Path

from opensac.sandbox.base import SandboxRequest, SandboxResult
from opensac.sandbox.docker_core import (
    SANDBOX_CONTRACT,
    BoundedProcessOutput,
    DockerImageContractVerifier,
    DockerSandboxCore,
    ExecutionWorkspace,
    SandboxImageContractError,
    broker_socket_mount_args,
    read_bounded_process_output,
    remove_docker_container,
)
from opensac.sandbox.validator import validate_code

__all__ = [
    "BoundedProcessOutput",
    "DockerImageContractVerifier",
    "DockerSandbox",
    "SANDBOX_CONTRACT",
    "SandboxImageContractError",
    "broker_socket_mount_args",
    "read_bounded_process_output",
]


class DockerSandbox(DockerSandboxCore):
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
        super().__init__(
            image=image,
            broker_socket=broker_socket,
            docker_host_platform=docker_host_platform,
            timeout_seconds=timeout_seconds,
            memory=memory,
            cpus=cpus,
            pids_limit=pids_limit,
            max_output_bytes=max_output_bytes,
        )

    def command(self, request: SandboxRequest, *, cid_path: Path | None = None) -> list[str]:
        cid_path = cid_path or self.broker_socket.parent / "containers" / "test.cid"
        command = self._docker_run_command(request, cid_path=cid_path)
        command += [
            "--env",
            f"OPENSAC_SESSION_TOKEN={request.session_token}",
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
        try:
            await self._image_contract.ensure_compatible()
        except SandboxImageContractError as exc:
            duration_seconds = time.monotonic() - validation_started
            return SandboxResult(
                exit_code=125,
                stdout="",
                stderr=str(exc),
                duration_seconds=duration_seconds,
                timings={
                    "validation_seconds": validation_seconds,
                    "workspace_setup_seconds": 0.0,
                    "startup_seconds": max(duration_seconds - validation_seconds, 0.0),
                    "program_seconds": 0.0,
                    "result_processing_seconds": 0.0,
                    "sandbox_total_seconds": duration_seconds,
                },
                launch_error=str(exc),
            )
        setup_started = time.monotonic()
        workspace = ExecutionWorkspace.prepare(request)
        containers_dir = self.broker_socket.parent / "containers"
        containers_dir.mkdir(parents=True, exist_ok=True)
        cid_path = containers_dir / f"{uuid.uuid4().hex}.cid"
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
                timeout_seconds=self._execution_timeout(request),
            )
        except asyncio.CancelledError:
            await self._remove_container(cid_path)
            raise
        if captured.timed_out or captured.output_limit_exceeded:
            await self._remove_container(cid_path)

        process_duration_seconds = time.monotonic() - started
        stdout = captured.stdout.decode("utf-8", errors="replace")
        stderr = captured.stderr.decode("utf-8", errors="replace")
        submitted, output_error = workspace.read_output(max_output_bytes=self.max_output_bytes)
        stderr += output_error
        duration_seconds = time.monotonic() - started
        startup_seconds = self._startup_seconds(
            workspace.ready_path,
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
            output_limit_exceeded=captured.output_limit_exceeded,
            timings={
                "validation_seconds": validation_seconds,
                "workspace_setup_seconds": workspace_setup_seconds,
                "startup_seconds": startup_seconds,
                "program_seconds": max(process_duration_seconds - startup_seconds, 0.0),
                "result_processing_seconds": max(duration_seconds - process_duration_seconds, 0.0),
                "sandbox_total_seconds": duration_seconds,
            },
            launch_error=self._launch_error(process.returncode, stderr),
        )
        cid_path.unlink(missing_ok=True)
        # Both files are per-execution now, so nothing later overwrites them and
        # they would otherwise pile up in a workspace the program has to keep
        # listing. The archived copy of the program lives outside the workspace.
        workspace.cleanup()
        return result

    @staticmethod
    async def _remove_container(cid_path: Path) -> None:
        if not cid_path.exists():
            return
        container_id = cid_path.read_text(encoding="utf-8").strip()
        if container_id:
            await remove_docker_container(container_id)
        cid_path.unlink(missing_ok=True)
