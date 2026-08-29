from __future__ import annotations

import asyncio
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import httpx
import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from opensac.agent.mcp import MCPConfig, OpenSACMCP
from opensac.agent.session import GenerationRegistry


class FakeOpenSAC:
    def __init__(
        self,
        *,
        exec_delay: float = 0,
        features: tuple[str, ...] = ("idempotent_exec",),
    ) -> None:
        self.exec_delay = exec_delay
        self.features = list(features)
        self.create_payloads: list[dict[str, Any]] = []
        self.exec_calls: list[tuple[str, str]] = []
        self.exec_payloads: list[tuple[str, dict[str, Any]]] = []
        self.executions: list[tuple[str, str]] = []
        self.deleted: list[str] = []
        self.sessions_by_request: dict[str, str] = {}
        self.execution_mode_by_session: dict[str, str] = {}
        self.workspace: dict[str, set[str]] = defaultdict(set)
        self.completed_execs: dict[tuple[str, str], tuple[str, dict[str, Any]]] = {}
        self.next_exec_failure: int | str | None = None
        self.exec_transport_failures_remaining = 0
        self.lose_next_exec_response_after_completion = False
        self.next_create_failure: int | str | None = None
        self.next_create_state_loss: str | None = None
        self.next_state_loss: str | None = None
        self.active_execs = 0
        self.max_active_execs = 0
        self.active_by_session: dict[str, int] = defaultdict(int)
        self.max_active_by_session: dict[str, int] = defaultdict(int)

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/sessions":
            payload = json.loads(request.content)
            self.create_payloads.append(payload)
            if self.next_create_state_loss is not None:
                state_loss = self.next_create_state_loss
                self.next_create_state_loss = None
                return httpx.Response(
                    410,
                    json={
                        "detail": {
                            "code": state_loss,
                            "message": "session state is gone",
                            "retryable": False,
                        }
                    },
                )
            if self.next_create_failure is not None:
                failure = self.next_create_failure
                self.next_create_failure = None
                if failure == "timeout":
                    raise httpx.ReadTimeout("temporary timeout", request=request)
                return httpx.Response(int(failure), json={"detail": "temporary failure"})
            request_id = payload["request_id"]
            session_id = self.sessions_by_request.setdefault(
                request_id, f"session-{len(self.sessions_by_request) + 1}"
            )
            self.execution_mode_by_session[session_id] = payload.get("execution_mode", "program")
            return httpx.Response(
                200,
                json={"id": session_id, "features": self.features},
            )

        if request.method == "POST" and request.url.path.endswith("/exec"):
            session_id = request.url.path.split("/")[3]
            exec_payload = json.loads(request.content)
            code = exec_payload["code"]
            exec_id = exec_payload.get("exec_id")
            self.exec_calls.append((session_id, code))
            self.exec_payloads.append((session_id, exec_payload))
            if exec_id is not None:
                completed = self.completed_execs.get((session_id, exec_id))
                if completed is not None:
                    previous_payload, result = completed
                    current_payload = json.dumps(
                        {key: value for key, value in exec_payload.items() if key != "exec_id"},
                        sort_keys=True,
                    )
                    if previous_payload != current_payload:
                        return httpx.Response(
                            409,
                            json={
                                "detail": {
                                    "code": "exec_id_conflict",
                                    "message": "execution id reused with another payload",
                                    "retryable": False,
                                }
                            },
                        )
                    return httpx.Response(200, json=result)
            if self.next_state_loss is not None:
                state_loss = self.next_state_loss
                self.next_state_loss = None
                return httpx.Response(
                    410,
                    json={
                        "detail": {
                            "code": state_loss,
                            "message": "session state is gone",
                            "retryable": False,
                        }
                    },
                )
            if self.next_exec_failure is not None:
                failure = self.next_exec_failure
                self.next_exec_failure = None
                if failure == "timeout":
                    raise httpx.ReadTimeout("temporary timeout", request=request)
                if failure == "exec_indeterminate":
                    return httpx.Response(
                        409,
                        json={
                            "detail": {
                                "code": failure,
                                "message": "execution outcome is unknown",
                                "retryable": False,
                            }
                        },
                    )
                return httpx.Response(int(failure), json={"detail": "temporary failure"})
            if self.exec_transport_failures_remaining:
                self.exec_transport_failures_remaining -= 1
                raise httpx.ReadTimeout("temporary timeout", request=request)

            self.active_execs += 1
            self.active_by_session[session_id] += 1
            self.max_active_execs = max(self.max_active_execs, self.active_execs)
            self.max_active_by_session[session_id] = max(
                self.max_active_by_session[session_id],
                self.active_by_session[session_id],
            )
            try:
                if self.exec_delay:
                    await asyncio.sleep(self.exec_delay)
                self.executions.append((session_id, code))
                if code == "write":
                    self.workspace[session_id].add("pool.jsonl")
                stdout = (
                    f"persisted={'pool.jsonl' in self.workspace[session_id]}"
                    if code == "read"
                    else "ok"
                )
                interpreter_lost = code == "lose-kernel"
                result = {
                    "exit_code": 0,
                    "duration_seconds": 0.01,
                    "stdout": stdout,
                    "stderr": "",
                    "output": None,
                    "citations": [],
                    "artifacts": sorted(self.workspace[session_id]),
                    "usage": {"search_calls": 0, "content_fetches": 0},
                    "error": None,
                    "execution_mode": self.execution_mode_by_session.get(session_id, "program"),
                    "interpreter_state": (
                        "lost"
                        if interpreter_lost
                        else "ready"
                        if self.execution_mode_by_session.get(session_id)
                        == "persistent_interpreter"
                        else "not_applicable"
                    ),
                    "interpreter_loss_reason": ("timeout" if interpreter_lost else None),
                    "namespace_symbol_count": (
                        3
                        if self.execution_mode_by_session.get(session_id)
                        == "persistent_interpreter"
                        else None
                    ),
                }
                if exec_id is not None:
                    request_payload = json.dumps(
                        {key: value for key, value in exec_payload.items() if key != "exec_id"},
                        sort_keys=True,
                    )
                    self.completed_execs[(session_id, exec_id)] = (request_payload, result)
                if self.lose_next_exec_response_after_completion:
                    self.lose_next_exec_response_after_completion = False
                    raise httpx.ReadTimeout("response lost after execution", request=request)
                return httpx.Response(200, json=result)
            finally:
                self.active_execs -= 1
                self.active_by_session[session_id] -= 1

        if request.method == "DELETE":
            self.deleted.append(request.url.path.split("/")[3])
            return httpx.Response(200, json={"status": "deleted"})
        return httpx.Response(404)


def _config(tmp_path: Path, *, api_key: str = "") -> MCPConfig:
    return MCPConfig(
        api_base="http://opensac.test",
        api_key=api_key,
        lease_seconds=3_600,
        state_dir=tmp_path / "state",
    )


def test_mcp_execution_mode_configuration_is_constrained(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="SAC_MCP_EXECUTION_MODE"):
        MCPConfig.from_env(
            {
                "SAC_MCP_STATE_DIR": str(tmp_path),
                "SAC_MCP_EXECUTION_MODE": "shared-idle-pool",
            }
        )


def _bridge(tmp_path: Path, server: FakeOpenSAC, *, api_key: str = "") -> OpenSACMCP:
    return OpenSACMCP(
        _config(tmp_path, api_key=api_key),
        transport=httpx.MockTransport(server),
    )


async def test_codex_context_reuses_and_isolates_sessions_without_leaking_ids(
    tmp_path: Path,
) -> None:
    server = FakeOpenSAC()
    bridge = _bridge(tmp_path, server, api_key="top-secret-api-key")
    try:
        first = await bridge.run_code("first", {"thread_id": "private-thread-a"})
        second = await bridge.run_code("second", {"thread_id": "private-thread-a"})
        third = await bridge.run_code("third", {"thread_id": "private-thread-b"})
    finally:
        await bridge.aclose()

    assert "exit_code=0" in first
    assert "private-thread" not in first + second + third
    assert len(server.create_payloads) == 2
    assert [payload["lease_seconds"] for payload in server.create_payloads] == [3_600, 3_600]
    request_ids = [payload["request_id"] for payload in server.create_payloads]
    assert all(request_id.startswith("agent:codex:") for request_id in request_ids)
    assert all("private-thread" not in request_id for request_id in request_ids)
    assert len(set(request_ids)) == 2
    assert [session_id for session_id, _ in server.exec_calls] == [
        "session-1",
        "session-1",
        "session-2",
    ]
    exec_ids = [payload["exec_id"] for _, payload in server.exec_payloads]
    assert len(set(exec_ids)) == 3
    assert all(exec_id.startswith("mcp:") for exec_id in exec_ids)
    assert all("private-thread" not in exec_id for exec_id in exec_ids)
    assert not server.deleted
    state_bytes = (_config(tmp_path).state_dir / "mcp_sessions.sqlite3").read_bytes()
    assert b"private-thread" not in state_bytes
    assert b"top-secret-api-key" not in state_bytes


async def test_missing_host_context_fails_closed_without_http(tmp_path: Path) -> None:
    server = FakeOpenSAC()
    bridge = _bridge(tmp_path, server)
    try:
        observation = await bridge.run_code("print('unsafe fallback')")
    finally:
        await bridge.aclose()

    assert "context_unavailable" in observation
    assert "No OpenSAC session was created" in observation
    assert not server.create_payloads
    assert not server.exec_calls


async def test_restart_resumes_same_leased_session_and_workspace(tmp_path: Path) -> None:
    server = FakeOpenSAC()
    first_bridge = _bridge(tmp_path, server)
    await first_bridge.run_code("write", {"thread_id": "thread-resume"})
    await first_bridge.aclose()

    second_bridge = _bridge(tmp_path, server)
    try:
        observation = await second_bridge.run_code("read", {"thread_id": "thread-resume"})
    finally:
        await second_bridge.aclose()

    assert "persisted=True" in observation
    assert len(server.create_payloads) == 2
    assert server.create_payloads[0]["request_id"] == server.create_payloads[1]["request_id"]
    assert [session_id for session_id, _ in server.exec_calls] == ["session-1", "session-1"]
    assert not server.deleted


@pytest.mark.parametrize("loss_code", ["session_expired", "worker_restarted", "interpreter_lost"])
async def test_state_loss_rotates_generation_without_replaying_code(
    tmp_path: Path, loss_code: str
) -> None:
    server = FakeOpenSAC()
    bridge = _bridge(tmp_path, server)
    try:
        await bridge.run_code("setup", {"thread_id": "thread-loss"})
        server.next_state_loss = loss_code
        lost = await bridge.run_code("must-not-replay", {"thread_id": "thread-loss"})
        assert [code for _, code in server.exec_calls].count("must-not-replay") == 1
        recovered = await bridge.run_code("fresh", {"thread_id": "thread-loss"})
    finally:
        await bridge.aclose()

    assert "state_lost" in lost
    assert "not replayed" in lost
    assert "exit_code=0" in recovered
    assert len(server.create_payloads) == 2
    assert server.create_payloads[0]["request_id"].endswith(":g1")
    assert server.create_payloads[1]["request_id"].endswith(":g2")


@pytest.mark.parametrize("failure", [429, 500, 503])
async def test_transient_failures_do_not_rotate_generation(
    tmp_path: Path, failure: int | str
) -> None:
    server = FakeOpenSAC()
    bridge = _bridge(tmp_path, server)
    try:
        await bridge.run_code("setup", {"thread_id": "thread-transient"})
        server.next_exec_failure = failure
        failed = await bridge.run_code("temporary", {"thread_id": "thread-transient"})
        recovered = await bridge.run_code("retry-later", {"thread_id": "thread-transient"})
    finally:
        await bridge.aclose()

    assert "failed" in failed.lower() or "timed out" in failed.lower()
    assert "session-1" not in failed
    assert "exit_code=0" in recovered
    assert len(server.create_payloads) == 1
    assert server.create_payloads[0]["request_id"].endswith(":g1")


async def test_lost_exec_response_retries_same_id_without_reexecution(
    tmp_path: Path,
) -> None:
    server = FakeOpenSAC()
    server.lose_next_exec_response_after_completion = True
    bridge = _bridge(tmp_path, server)
    try:
        observation = await bridge.run_code(
            "write",
            {"thread_id": "thread-response-loss"},
            invocation_id="mcp-request-42",
        )
    finally:
        await bridge.aclose()

    assert "exit_code=0" in observation
    assert server.exec_calls == [("session-1", "write"), ("session-1", "write")]
    assert server.executions == [("session-1", "write")]
    exec_ids = [payload["exec_id"] for _, payload in server.exec_payloads]
    assert len(exec_ids) == 2
    assert exec_ids[0] == exec_ids[1]
    assert "mcp-request-42" not in exec_ids[0]
    assert "thread-response-loss" not in exec_ids[0]


async def test_exhausted_exec_transport_retries_report_unknown_outcome(
    tmp_path: Path,
) -> None:
    server = FakeOpenSAC()
    server.exec_transport_failures_remaining = 2
    bridge = _bridge(tmp_path, server)
    try:
        observation = await bridge.run_code(
            "print('maybe')",
            {"thread_id": "thread-timeout"},
            invocation_id="mcp-request-43",
        )
    finally:
        await bridge.aclose()

    assert "execution_outcome_unknown" in observation
    assert "must not be rerun automatically" in observation
    assert len(server.exec_calls) == 2
    assert server.exec_payloads[0][1]["exec_id"] == server.exec_payloads[1][1]["exec_id"]


async def test_exec_contract_errors_are_explicit_and_not_retried(tmp_path: Path) -> None:
    server = FakeOpenSAC()
    bridge = _bridge(tmp_path, server)
    try:
        server.next_exec_failure = "exec_indeterminate"
        indeterminate = await bridge.run_code(
            "first",
            {"thread_id": "thread-contract-errors"},
            invocation_id="mcp-request-44",
        )
        await bridge.run_code(
            "second",
            {"thread_id": "thread-contract-errors"},
            invocation_id="mcp-request-45",
        )
        conflict = await bridge.run_code(
            "different",
            {"thread_id": "thread-contract-errors"},
            invocation_id="mcp-request-45",
        )
    finally:
        await bridge.aclose()

    assert "exec_indeterminate" in indeterminate
    assert "not replayed" in indeterminate
    assert "exec_id_conflict" in conflict
    assert "was not run" in conflict
    assert [code for _, code in server.exec_calls].count("first") == 1
    assert [code for _, code in server.exec_calls].count("different") == 1


async def test_mcp_refuses_unsafe_exec_against_older_server(tmp_path: Path) -> None:
    server = FakeOpenSAC(features=())
    bridge = _bridge(tmp_path, server)
    try:
        observation = await bridge.run_code(
            "print('unsafe')",
            {"thread_id": "thread-old-server"},
        )
    finally:
        await bridge.aclose()

    assert "idempotent_exec_unavailable" in observation
    assert "was not run" in observation
    assert not server.exec_calls


async def test_mcp_requests_persistent_mode_and_reports_it(tmp_path: Path) -> None:
    server = FakeOpenSAC()
    config = MCPConfig(
        api_base="http://opensac.test",
        api_key="",
        lease_seconds=3_600,
        state_dir=tmp_path / "state",
        execution_mode="persistent_interpreter",
    )
    bridge = OpenSACMCP(config, transport=httpx.MockTransport(server))
    try:
        observation = await bridge.run_code("value = 1", {"thread_id": "repl"})
    finally:
        await bridge.aclose()

    assert server.create_payloads[0]["execution_mode"] == "persistent_interpreter"
    assert "execution_mode=persistent_interpreter" in observation
    assert "interpreter_state=ready" in observation
    assert "namespace_symbols=3" in observation


async def test_lost_interpreter_result_rotates_without_replaying_cell(tmp_path: Path) -> None:
    server = FakeOpenSAC()
    config = MCPConfig(
        api_base="http://opensac.test",
        api_key="",
        lease_seconds=3_600,
        state_dir=tmp_path / "state",
        execution_mode="persistent_interpreter",
    )
    bridge = OpenSACMCP(config, transport=httpx.MockTransport(server))
    try:
        lost = await bridge.run_code("lose-kernel", {"thread_id": "repl-loss"})
        recovered = await bridge.run_code("fresh", {"thread_id": "repl-loss"})
    finally:
        await bridge.aclose()

    assert "state_lost" in lost
    assert "will not be replayed" in lost
    assert [code for _, code in server.exec_calls].count("lose-kernel") == 1
    assert "interpreter_state=ready" in recovered
    assert len(server.create_payloads) == 2
    assert len(server.deleted) == 1


async def test_restart_detects_state_loss_during_idempotent_create(tmp_path: Path) -> None:
    server = FakeOpenSAC()
    first_bridge = _bridge(tmp_path, server)
    await first_bridge.run_code("setup", {"thread_id": "thread-create-loss"})
    await first_bridge.aclose()

    server.next_create_state_loss = "worker_restarted"
    second_bridge = _bridge(tmp_path, server)
    try:
        lost = await second_bridge.run_code("must-not-replay", {"thread_id": "thread-create-loss"})
        recovered = await second_bridge.run_code("fresh", {"thread_id": "thread-create-loss"})
    finally:
        await second_bridge.aclose()

    assert "state_lost" in lost
    assert "exit_code=0" in recovered
    assert [code for _, code in server.exec_calls] == ["setup", "fresh"]
    request_ids = [payload["request_id"] for payload in server.create_payloads]
    assert request_ids[0] == request_ids[1]
    assert request_ids[0].endswith(":g1")
    assert request_ids[2].endswith(":g2")


@pytest.mark.parametrize("failure", [429, 500, 503, "timeout"])
async def test_transient_create_failures_reuse_generation(
    tmp_path: Path, failure: int | str
) -> None:
    server = FakeOpenSAC()
    server.next_create_failure = failure
    bridge = _bridge(tmp_path, server)
    try:
        failed = await bridge.run_code("first", {"thread_id": "thread-create-transient"})
        recovered = await bridge.run_code("retry-later", {"thread_id": "thread-create-transient"})
    finally:
        await bridge.aclose()

    assert "failed" in failed.lower() or "timed out" in failed.lower()
    assert "exit_code=0" in recovered
    request_ids = [payload["request_id"] for payload in server.create_payloads]
    assert request_ids[0] == request_ids[1]
    assert request_ids[0].endswith(":g1")
    assert [code for _, code in server.exec_calls] == ["retry-later"]


async def test_same_context_serializes_while_different_contexts_run_in_parallel(
    tmp_path: Path,
) -> None:
    server = FakeOpenSAC(exec_delay=0.03)
    bridge = _bridge(tmp_path, server)
    try:
        await asyncio.gather(
            bridge.run_code("same-1", {"thread_id": "same"}),
            bridge.run_code("same-2", {"thread_id": "same"}),
        )
        same_session = server.exec_calls[0][0]
        assert server.max_active_by_session[same_session] == 1

        server.max_active_execs = 0
        await asyncio.gather(
            bridge.run_code("other-a", {"thread_id": "other-a"}),
            bridge.run_code("other-b", {"thread_id": "other-b"}),
        )
    finally:
        await bridge.aclose()

    assert server.max_active_execs == 2


def test_generation_registry_concurrent_loss_observers_converge(tmp_path: Path) -> None:
    first = GenerationRegistry(tmp_path / "state")
    second = GenerationRegistry(tmp_path / "state")
    try:
        assert first.generation("hash") == 1
        assert first.advance("hash", 1) == 2
        assert second.advance("hash", 1) == 2
        assert second.generation("hash") == 2
    finally:
        first.close()
        second.close()


def test_mcp_adapter_does_not_depend_on_custom_agent_package() -> None:
    module_path = Path(__file__).parents[1] / "src" / "opensac" / "agent" / "mcp.py"

    assert "sac_agent" not in module_path.read_text(encoding="utf-8")


async def test_stdio_handshake_exposes_code_only_sac_run_schema(tmp_path: Path) -> None:
    error_log_path = tmp_path / "mcp-stderr.log"
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "opensac.agent.mcp"],
        env={"SAC_MCP_STATE_DIR": str(tmp_path / "stdio-state")},
        cwd=str(Path(__file__).parents[1]),
    )
    with error_log_path.open("w", encoding="utf-8") as error_log:
        async with (
            stdio_client(parameters, errlog=error_log) as streams,
            ClientSession(*streams) as session,
        ):
            await session.initialize()
            tools = {tool.name: tool for tool in (await session.list_tools()).tools}
            assert set(tools) == {"sac_run"}
            sac_run = tools["sac_run"]
            assert set(sac_run.inputSchema["properties"]) == {"code"}
            assert sac_run.inputSchema["required"] == ["code"]
            assert "session_id" not in json.dumps(sac_run.inputSchema)
            result = await session.call_tool("sac_run", {"code": "print('no context')"})
            assert not result.isError
            assert "context_unavailable" in result.content[0].text  # type: ignore[union-attr]

    assert not error_log_path.read_text(encoding="utf-8")
