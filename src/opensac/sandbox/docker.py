from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from pathlib import Path

from opensac.sandbox.base import SandboxRequest, SandboxResult
from opensac.sandbox.validator import validate_code


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
        return [
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
            "--cpus",
            str(self.cpus),
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
            "OPENSAC_OUTPUT_PATH=/workspace/.opensac-output.json",
            self.image,
            "/workspace/.opensac-program.py",
        ]

    async def execute(self, request: SandboxRequest) -> SandboxResult:
        validate_code(request.code)
        request.workspace.mkdir(parents=True, exist_ok=True)
        program_path = request.workspace / ".opensac-program.py"
        output_path = request.workspace / ".opensac-output.json"
        containers_dir = self.broker_socket.parent / "containers"
        containers_dir.mkdir(parents=True, exist_ok=True)
        cid_path = containers_dir / f"{uuid.uuid4().hex}.cid"
        program_path.write_text(request.code, encoding="utf-8")
        output_path.unlink(missing_ok=True)
        cid_path.unlink(missing_ok=True)
        started = time.monotonic()
        process = await asyncio.create_subprocess_exec(
            *self.command(request, cid_path=cid_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        timed_out = False
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(),
                timeout=self.timeout_seconds,
            )
        except TimeoutError:
            timed_out = True
            process.kill()
            stdout_bytes, stderr_bytes = await process.communicate()
            await self._remove_container(cid_path)
        except asyncio.CancelledError:
            process.kill()
            await process.communicate()
            await self._remove_container(cid_path)
            raise

        stdout = stdout_bytes[: self.max_output_bytes].decode("utf-8", errors="replace")
        stderr = stderr_bytes[: self.max_output_bytes].decode("utf-8", errors="replace")
        submitted = {}
        if output_path.exists() and output_path.stat().st_size <= self.max_output_bytes:
            try:
                submitted = json.loads(output_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                stderr += "\nOpenSAC output was not valid JSON."
        result = SandboxResult(
            exit_code=process.returncode if process.returncode is not None else -1,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=time.monotonic() - started,
            output=submitted.get("output"),
            citations=submitted.get("citations", []),
            timed_out=timed_out,
        )
        cid_path.unlink(missing_ok=True)
        return result

    @staticmethod
    async def _remove_container(cid_path: Path) -> None:
        if not cid_path.exists():
            return
        container_id = cid_path.read_text(encoding="utf-8").strip()
        if container_id:
            cleanup = await asyncio.create_subprocess_exec(
                "docker",
                "rm",
                "--force",
                container_id,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await cleanup.wait()
        cid_path.unlink(missing_ok=True)
