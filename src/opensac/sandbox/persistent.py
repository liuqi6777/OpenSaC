from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from opensac.sandbox.base import SandboxRequest, SandboxResult
from opensac.sandbox.warm import SessionLike, WarmDockerSandbox


class PersistentDockerSandbox(WarmDockerSandbox):
    """One pinned Python interpreter container per OpenSAC session."""

    _SANDBOX_LABEL = "persistent-interpreter"
    _KERNEL = "/opt/runner/kernel.py"
    _RELAY = "/opt/runner/relay.py"

    def __init__(self, **kwargs: Any) -> None:
        # A persistent namespace cannot participate in idle or LRU eviction.
        kwargs["idle_timeout_seconds"] = 0.0
        kwargs["max_containers"] = 0
        super().__init__(**kwargs)
        self._ready_keys: set[str] = set()

    def snapshot(self) -> dict[str, int]:
        values = super().snapshot()
        values["ready"] = len(self._ready_keys)
        return values

    def container_command(
        self,
        request: SandboxRequest,
        *,
        cid_path: Path | None = None,
    ) -> list[str]:
        cid_path = cid_path or self.broker_socket.parent / "containers" / "repl-test.cid"
        session_digest = hashlib.sha256(self._session_key(request).encode()).hexdigest()[:16]
        command = self._docker_run_command(
            request,
            cid_path=cid_path,
            detach=True,
            init=True,
            extra_args=(
                "--label",
                f"opensac.sandbox={self._SANDBOX_LABEL}",
                "--label",
                f"opensac.owner={self.owner_label}",
                "--label",
                f"opensac.session={session_digest}",
            ),
        )
        command += [
            "--workdir",
            "/workspace",
            "--entrypoint",
            "python",
            self.image,
            "-I",
            self._KERNEL,
        ]
        return command

    def execution_command(self, container_id: str, request: SandboxRequest) -> list[str]:
        workspace = self.container_execution_workspace(request)
        command = [
            "docker",
            "exec",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--workdir",
            workspace,
            "--env",
            f"OPENSAC_SESSION_TOKEN={request.session_token}",
            "--env",
            f"OPENSAC_OUTPUT_PATH={workspace}/{request.output_filename}",
            "--env",
            f"OPENSAC_READY_PATH={workspace}/{request.output_filename}.ready",
            "--env",
            f"OPENSAC_WORKSPACE={workspace}",
            "--env",
            f"OPENSAC_KERNEL_RESULT_PATH={workspace}/{request.kernel_result_filename}",
        ]
        if request.execution_id:
            command += ["--env", f"OPENSAC_EXECUTION_ID={request.execution_id}"]
        command += [
            container_id,
            "python",
            "-I",
            self._RELAY,
            f"{workspace}/{request.program_filename}",
        ]
        return command

    @staticmethod
    def _read_kernel_result(path: Path) -> dict[str, Any] | None:
        try:
            if path.stat().st_size > 64 * 1024:
                return None
            value = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError):
            return None
        if (
            not isinstance(value, dict)
            or value.get("protocol") != 1
            or value.get("complete") is not True
            or not isinstance(value.get("exit_code"), int)
            or not 0 <= value["exit_code"] <= 255
            or not isinstance(value.get("namespace_symbol_count"), int)
            or value["namespace_symbol_count"] < 0
            or (
                value.get("interpreter_loss_reason") is not None
                and not isinstance(value["interpreter_loss_reason"], str)
            )
        ):
            return None
        return value

    @staticmethod
    def _missing_result_loss_reason(result: SandboxResult) -> str:
        failure = f"{result.stderr}\n{result.launch_error or ''}".lower()
        kernel_exit_markers = (
            "persistent interpreter disconnected",
            "warm sandbox process audit failed",
        )
        if any(marker in failure for marker in kernel_exit_markers):
            return "kernel_exit"
        return "kernel_protocol_error"

    async def execute(self, request: SandboxRequest) -> SandboxResult:
        key = self._session_key(request)
        ready_before = key in self._ready_keys
        metadata_path = request.workspace / request.kernel_result_filename
        metadata_path.unlink(missing_ok=True)
        result = await super().execute(request)
        metadata = self._read_kernel_result(metadata_path)
        metadata_path.unlink(missing_ok=True)

        loss_reason: str | None = None
        if result.timed_out:
            loss_reason = "timeout"
        elif result.output_limit_exceeded:
            loss_reason = "output_limit"
        elif metadata is not None:
            result.namespace_symbol_count = metadata["namespace_symbol_count"]
            raw_reason = metadata.get("interpreter_loss_reason")
            if raw_reason:
                loss_reason = str(raw_reason)[:256]
            elif metadata["exit_code"] != result.exit_code:
                loss_reason = "kernel_protocol_error"
            elif result.launch_error is not None:
                loss_reason = "kernel_exit"
        elif result.execution_started or ready_before:
            loss_reason = self._missing_result_loss_reason(result)

        if loss_reason is not None:
            result.interpreter_state = "lost"
            result.interpreter_loss_reason = loss_reason
            self._ready_keys.discard(key)
            await self._close_key(key)
            return result

        if metadata is not None:
            result.interpreter_state = "ready"
            self._ready_keys.add(key)
            return result

        # No user code started and no prior namespace existed. Drop a possibly
        # half-started container, but keep the session retryable.
        result.interpreter_state = "not_started"
        if result.launch_error is None:
            first_line = result.stderr.strip().splitlines()[0] if result.stderr.strip() else ""
            detail = first_line or "persistent interpreter did not return a result"
            result.launch_error = f"The persistent interpreter could not be started: {detail}"
        await self._close_key(key)
        return result

    async def reap_idle(self, max_idle_seconds: float | None = None) -> int:
        return 0

    async def close_session(self, session: SessionLike) -> None:
        await super().close_session(session)
        self._ready_keys.discard(str(session.id))
        self._ready_keys.discard(str(session.token))

    async def close(self) -> None:
        await super().close()
        self._ready_keys.clear()
