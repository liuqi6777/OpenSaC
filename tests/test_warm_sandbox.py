from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from opensac.sandbox import SandboxRequest, WarmDockerSandbox


class _FakeProcess:
    def __init__(
        self,
        returncode: int = 0,
        stdout: bytes = b"",
        stderr: bytes = b"",
        gate: asyncio.Event | None = None,
    ) -> None:
        self.returncode: int | None = None
        self._final_returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self._gate = gate
        self._killed = False
        self.stdout = _FakeStream(stdout, gate)
        self.stderr = _FakeStream(stderr, gate)

    async def communicate(self) -> tuple[bytes, bytes]:
        if self._gate is not None:
            await self._gate.wait()
        self.returncode = -9 if self._killed else self._final_returncode
        return self._stdout, self._stderr

    def kill(self) -> None:
        if self.returncode is not None:
            raise ProcessLookupError
        self._killed = True
        if self._gate is not None:
            self._gate.set()

    async def wait(self) -> int:
        if self._gate is not None:
            await self._gate.wait()
        self.returncode = -9 if self._killed else self._final_returncode
        return self.returncode


class _FakeStream:
    def __init__(self, content: bytes, gate: asyncio.Event | None) -> None:
        self.content = bytearray(content)
        self.gate = gate

    async def read(self, size: int) -> bytes:
        if self.gate is not None:
            await self.gate.wait()
        if not self.content:
            return b""
        chunk = bytes(self.content[:size])
        del self.content[:size]
        return chunk


class _FakeDocker:
    def __init__(
        self,
        *,
        exec_gates: list[asyncio.Event | None] | None = None,
        exec_outputs: list[bytes] | None = None,
        top_outputs: list[bytes] | None = None,
        ps_outputs: list[bytes] | None = None,
        rm_results: list[tuple[int, bytes]] | None = None,
        image_contract: bytes = b"3\n",
    ) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.exec_gates = list(exec_gates or [])
        self.exec_started = asyncio.Event()
        self.container_count = 0
        self.exec_count = 0
        self.on_exec: Any = None
        self.top_outputs = list(top_outputs or [])
        self.exec_outputs = list(exec_outputs or [])
        self.ps_outputs = list(ps_outputs or [])
        self.rm_results = list(rm_results or [])
        self.image_contract = image_contract

    async def __call__(self, *command: str, **_: Any) -> _FakeProcess:
        self.calls.append(command)
        operation = command[1]
        if operation == "image":
            assert command[2] == "inspect"
            return _FakeProcess(stdout=self.image_contract)
        if operation == "run":
            self.container_count += 1
            container_id = f"warm-container-{self.container_count}"
            cid_path = Path(command[command.index("--cidfile") + 1])
            cid_path.write_text(container_id, encoding="utf-8")
            return _FakeProcess(stdout=f"{container_id}\n".encode())
        if operation == "exec":
            index = self.exec_count
            self.exec_count += 1
            self.exec_started.set()
            if self.on_exec is not None:
                self.on_exec(command, index)
            gate = self.exec_gates[index] if index < len(self.exec_gates) else None
            stdout = (
                self.exec_outputs.pop(0)
                if self.exec_outputs
                else f"exec-{index}\n".encode()
            )
            return _FakeProcess(stdout=stdout, gate=gate)
        if operation == "top":
            stdout = self.top_outputs.pop(0) if self.top_outputs else b"PID\n100\n101\n"
            return _FakeProcess(stdout=stdout)
        if operation == "ps":
            stdout = self.ps_outputs.pop(0) if self.ps_outputs else b""
            return _FakeProcess(stdout=stdout)
        if operation == "rm":
            returncode, stderr = self.rm_results.pop(0) if self.rm_results else (0, b"")
            return _FakeProcess(returncode=returncode, stderr=stderr)
        raise AssertionError(f"unexpected docker command: {command}")

    def operations(self, name: str) -> list[tuple[str, ...]]:
        return [call for call in self.calls if call[1] == name]


def _sandbox(tmp_path: Path, **kwargs: Any) -> WarmDockerSandbox:
    socket = tmp_path / "broker.sock"
    socket.touch(exist_ok=True)
    return WarmDockerSandbox(image="opensac-test", broker_socket=socket, **kwargs)


def _request(
    tmp_path: Path,
    *,
    session_id: str = "sess-1",
    token: str = "token-1",
    sequence: int = 1,
) -> SandboxRequest:
    return SandboxRequest(
        code="print('ok')",
        workspace=tmp_path / "workspace",
        session_token=token,
        execution_id=f"exec-{sequence}",
        program_filename=f".opensac-program-{sequence:03d}.py",
        output_filename=f".opensac-output-{sequence:03d}.json",
        session_id=session_id,
    )


def test_warm_commands_keep_security_flags_and_inject_credentials_per_exec(
    tmp_path: Path,
) -> None:
    sandbox = _sandbox(tmp_path)
    request = _request(tmp_path)

    container = sandbox.container_command(request)
    joined = " ".join(container)
    assert "--network none" in joined
    assert "--read-only" in container
    assert "--cap-drop ALL" in joined
    assert "no-new-privileges" in joined
    assert "--init" in container
    assert f"opensac.owner={sandbox.owner_label}" in container
    assert "token-1" not in joined
    assert container[-4:] == ["opensac-test", "-I", "-c", sandbox._KEEPALIVE]

    execution = sandbox.execution_command("container-id", request)
    assert "OPENSAC_SESSION_TOKEN=token-1" in execution
    assert "OPENSAC_EXECUTION_ID=exec-1" in execution
    assert execution[-4:] == [
        "python",
        "-I",
        "/opt/runner/entrypoint.py",
        "/workspace/.opensac-program-001.py",
    ]
    assert execution[-1] == "/workspace/.opensac-program-001.py"


async def test_reap_orphans_only_targets_this_broker_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeDocker(ps_outputs=[b"orphan-1\norphan-2\n"])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)
    sandbox = _sandbox(tmp_path)

    assert await sandbox.reap_orphans() == 2
    ps_command = fake.operations("ps")[0]
    assert "--no-trunc" in ps_command
    assert f"label=opensac.owner={sandbox.owner_label}" in ps_command
    assert [command[-1] for command in fake.operations("rm")] == [
        "orphan-1",
        "orphan-2",
    ]


async def test_container_is_lazy_reused_by_session_id_and_closed_with_session_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeDocker()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)
    sandbox = _sandbox(tmp_path)

    first = await sandbox.execute(_request(tmp_path, token="run-token-1", sequence=1))
    second = await sandbox.execute(_request(tmp_path, token="run-token-2", sequence=2))

    assert first.succeeded and second.succeeded
    assert len(fake.operations("image")) == 1
    assert len(fake.operations("run")) == 1
    assert len(fake.operations("exec")) == 2
    assert "OPENSAC_SESSION_TOKEN=run-token-1" in fake.operations("exec")[0]
    assert "OPENSAC_SESSION_TOKEN=run-token-2" in fake.operations("exec")[1]

    session = SimpleNamespace(id="sess-1", token="run-token-2", workspace=str(tmp_path))
    await sandbox.close_session(session)
    assert len(fake.operations("rm")) == 1


async def test_warm_sandbox_reports_stale_image_as_launch_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeDocker(image_contract=b"2\n")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)
    sandbox = _sandbox(tmp_path)

    result = await sandbox.execute(_request(tmp_path))

    assert result.exit_code == 125
    assert "has contract '2'; expected 3" in (result.launch_error or "")
    assert fake.operations("run") == []


async def test_same_session_executes_serially_and_starts_only_one_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release_first = asyncio.Event()
    fake = _FakeDocker(exec_gates=[release_first, None])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)
    sandbox = _sandbox(tmp_path)

    first = asyncio.create_task(sandbox.execute(_request(tmp_path, sequence=1)))
    await fake.exec_started.wait()
    second = asyncio.create_task(sandbox.execute(_request(tmp_path, sequence=2)))
    await asyncio.sleep(0)

    assert len(fake.operations("run")) == 1
    assert len(fake.operations("exec")) == 1
    release_first.set()
    results = await asyncio.gather(first, second)
    assert all(result.succeeded for result in results)
    assert len(fake.operations("exec")) == 2
    await sandbox.close()


async def test_close_session_waits_for_inflight_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = asyncio.Event()
    fake = _FakeDocker(exec_gates=[release])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)
    sandbox = _sandbox(tmp_path)

    execution = asyncio.create_task(sandbox.execute(_request(tmp_path)))
    await fake.exec_started.wait()
    session = SimpleNamespace(id="sess-1", token="token-1", workspace=str(tmp_path))
    closing = asyncio.create_task(sandbox.close_session(session))
    await asyncio.sleep(0)

    assert fake.operations("rm") == []
    with pytest.raises(RuntimeError, match="is closed"):
        await sandbox.execute(_request(tmp_path, sequence=2))
    release.set()
    assert (await execution).succeeded
    await closing
    assert len(fake.operations("rm")) == 1
    assert len(fake.operations("run")) == 1


async def test_timeout_drops_container_before_next_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    never_release = asyncio.Event()
    fake = _FakeDocker(exec_gates=[never_release, None])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)
    sandbox = _sandbox(tmp_path, timeout_seconds=0.001)

    timed_out = await sandbox.execute(_request(tmp_path, sequence=1))
    recovered = await sandbox.execute(_request(tmp_path, sequence=2))

    assert timed_out.timed_out
    assert not timed_out.succeeded
    assert recovered.succeeded
    assert len(fake.operations("run")) == 2
    assert len(fake.operations("rm")) == 1
    await sandbox.close()


async def test_output_contract_and_cleanup_match_cold_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeDocker()
    request = _request(tmp_path)

    def write_output(_: tuple[str, ...], __: int) -> None:
        output_path = request.workspace / request.output_filename
        output_path.write_text(
            '{"output": {"answer": 42}, "citations": [{"ref": "r1"}]}',
            encoding="utf-8",
        )

    fake.on_exec = write_output
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)
    sandbox = _sandbox(tmp_path)

    result = await sandbox.execute(request)

    assert result.output == {"answer": 42}
    assert result.citations == [{"ref": "r1"}]
    assert result.stdout == "exec-0\n"
    assert not (request.workspace / request.program_filename).exists()
    assert not (request.workspace / request.output_filename).exists()
    await sandbox.close()


async def test_reap_idle_uses_last_completed_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeDocker()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)
    now = [100.0]
    monkeypatch.setattr("opensac.sandbox.warm.time.monotonic", lambda: now[0])
    sandbox = _sandbox(tmp_path)
    await sandbox.execute(_request(tmp_path))

    now[0] = 105.0
    assert await sandbox.reap_idle(max_idle_seconds=10.0) == 0
    now[0] = 111.0
    assert await sandbox.reap_idle(max_idle_seconds=10.0) == 1
    assert len(fake.operations("rm")) == 1


async def test_warm_container_limit_evicts_only_idle_lru_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = asyncio.Event()
    fake = _FakeDocker(exec_gates=[release, None])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)
    sandbox = _sandbox(tmp_path, max_containers=1)

    first = asyncio.create_task(
        sandbox.execute(_request(tmp_path, session_id="sess-1", sequence=1))
    )
    await fake.exec_started.wait()
    second = asyncio.create_task(
        sandbox.execute(_request(tmp_path, session_id="sess-2", sequence=2))
    )
    await asyncio.sleep(0)

    assert len(fake.operations("run")) == 1
    assert fake.operations("rm") == []
    assert sandbox.snapshot()["containers"] == 1
    assert sandbox.snapshot()["capacity"] == 1
    assert sandbox.snapshot()["active"] == 1
    assert sandbox.snapshot()["waiting"] == 1
    release.set()
    first_result, second_result = await asyncio.gather(first, second)

    assert first_result.succeeded and second_result.succeeded
    assert len(fake.operations("run")) == 2
    assert [command[-1] for command in fake.operations("rm")] == [
        "warm-container-1"
    ]
    assert sandbox.snapshot()["containers"] == 1
    assert sandbox.snapshot()["limit"] == 1
    assert sandbox.snapshot()["waiting"] == 0
    await sandbox.close()


async def test_background_descendant_poisons_container_and_next_exec_is_fresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    baseline = b"PID\n100\n101\n"
    fake = _FakeDocker(
        top_outputs=[baseline, b"PID\n100\n101\n202\n", baseline, baseline]
    )
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)
    sandbox = _sandbox(tmp_path)

    contaminated = await sandbox.execute(_request(tmp_path, sequence=1))
    recovered = await sandbox.execute(_request(tmp_path, sequence=2))

    assert contaminated.succeeded is False
    assert "process audit failed" in (contaminated.launch_error or "")
    assert recovered.succeeded is True
    assert len(fake.operations("run")) == 2
    assert len(fake.operations("rm")) == 1
    await sandbox.close()


async def test_output_limit_discards_whole_warm_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeDocker(exec_outputs=[b"x" * 4097, b"ok\n"])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)
    sandbox = _sandbox(tmp_path, max_output_bytes=4096)

    limited = await sandbox.execute(_request(tmp_path, sequence=1))
    recovered = await sandbox.execute(_request(tmp_path, sequence=2))

    assert len(limited.stdout.encode()) == 4096
    assert "reached the output limit" in limited.stderr
    assert recovered.succeeded is True
    assert len(fake.operations("run")) == 2
    assert len(fake.operations("rm")) == 1
    await sandbox.close()


async def test_failed_rm_retains_poisoned_state_and_close_can_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeDocker(rm_results=[(1, b"daemon unavailable"), (0, b"")])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)
    sandbox = _sandbox(tmp_path)
    await sandbox.execute(_request(tmp_path))
    session = SimpleNamespace(id="sess-1", token="token-1", workspace=str(tmp_path))

    with pytest.raises(RuntimeError, match="daemon unavailable"):
        await sandbox.close_session(session)

    state = sandbox._sessions["sess-1"]
    assert state.container_id == "warm-container-1"
    assert state.poisoned is True
    assert state.closing is False

    await sandbox.close_session(session)
    assert "sess-1" not in sandbox._sessions
    assert [command[-1] for command in fake.operations("rm")] == [
        "warm-container-1",
        "warm-container-1",
    ]


async def test_empty_registry_closes_labeled_orphan_and_propagates_rm_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeDocker(
        ps_outputs=[b"crash-orphan\n", b"", b"crash-orphan\n", b""],
        rm_results=[(1, b"daemon unavailable"), (0, b"")],
    )
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)
    sandbox = _sandbox(tmp_path)
    session = SimpleNamespace(id="sess-1", token="token-1", workspace=str(tmp_path))

    with pytest.raises(RuntimeError, match="daemon unavailable"):
        await sandbox.close_session(session)
    await sandbox.close_session(session)

    assert [command[-1] for command in fake.operations("rm")] == [
        "crash-orphan",
        "crash-orphan",
    ]
    assert all("--no-trunc" in command for command in fake.operations("ps"))
