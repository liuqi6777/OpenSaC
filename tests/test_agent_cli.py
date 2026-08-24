from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

import httpx
from typer.testing import CliRunner

from opensac import cli
from opensac.agent import cli as agent_cli


class FakeOpenSAC:
    def __init__(self) -> None:
        self.create_payloads: list[dict[str, Any]] = []
        self.exec_calls: list[tuple[str, str]] = []
        self.deleted: list[str] = []
        self.sessions_by_request: dict[str, str] = {}
        self.execution_mode_by_session: dict[str, str] = {}
        self.workspace: dict[str, set[str]] = defaultdict(set)
        self.next_state_loss: str | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/sessions":
            payload = json.loads(request.content)
            self.create_payloads.append(payload)
            request_id = payload["request_id"]
            session_id = self.sessions_by_request.setdefault(
                request_id, f"session-{len(self.sessions_by_request) + 1}"
            )
            self.execution_mode_by_session[session_id] = payload.get("execution_mode", "program")
            return httpx.Response(200, json={"id": session_id})
        if request.method == "POST" and request.url.path.endswith("/exec"):
            session_id = request.url.path.split("/")[3]
            code = json.loads(request.content)["code"]
            self.exec_calls.append((session_id, code))
            if self.next_state_loss is not None:
                loss_code = self.next_state_loss
                self.next_state_loss = None
                return httpx.Response(
                    410,
                    json={
                        "detail": {
                            "code": loss_code,
                            "message": "session state is gone",
                            "retryable": False,
                        }
                    },
                )
            if code == "write":
                self.workspace[session_id].add("pool.jsonl")
            stdout = (
                f"persisted={'pool.jsonl' in self.workspace[session_id]}"
                if code == "read"
                else "ok"
            )
            return httpx.Response(
                200,
                json={
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
                        "ready"
                        if self.execution_mode_by_session.get(session_id)
                        == "persistent_interpreter"
                        else "not_applicable"
                    ),
                    "namespace_symbol_count": (
                        3
                        if self.execution_mode_by_session.get(session_id)
                        == "persistent_interpreter"
                        else None
                    ),
                },
            )
        if request.method == "DELETE":
            self.deleted.append(request.url.path.split("/")[3])
            return httpx.Response(200)
        return httpx.Response(404)


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    return {
        "SAC_API_BASE": "http://opensac.test",
        "SAC_API_KEY": "top-secret-api-key",
        "SAC_CLI_STATE_DIR": str(tmp_path / "state"),
        **extra,
    }


async def test_codex_cli_calls_resume_one_session_without_exposing_context(
    tmp_path: Path,
) -> None:
    server = FakeOpenSAC()
    env = _env(tmp_path, CODEX_THREAD_ID="private-codex-thread")
    transport = httpx.MockTransport(server)

    first = await agent_cli.run_cli_code("write", environ=env, transport=transport)
    second = await agent_cli.run_cli_code("read", environ=env, transport=transport)

    assert "exit_code=0" in first
    assert "persisted=True" in second
    assert len(server.create_payloads) == 2
    assert server.create_payloads[0]["request_id"] == server.create_payloads[1]["request_id"]
    assert server.create_payloads[0]["request_id"].startswith("agent:codex-cli:")
    assert "private-codex-thread" not in server.create_payloads[0]["request_id"]
    assert [session_id for session_id, _ in server.exec_calls] == ["session-1", "session-1"]
    assert not server.deleted
    state_bytes = (tmp_path / "state" / "cli_sessions.sqlite3").read_bytes()
    assert b"private-codex-thread" not in state_bytes
    assert b"top-secret-api-key" not in state_bytes


async def test_claude_cli_uses_official_bash_session_environment(tmp_path: Path) -> None:
    server = FakeOpenSAC()
    observation = await agent_cli.run_cli_code(
        "work",
        environ=_env(tmp_path, CLAUDE_CODE_SESSION_ID="private-claude-session"),
        transport=httpx.MockTransport(server),
    )

    assert "exit_code=0" in observation
    request_id = server.create_payloads[0]["request_id"]
    assert request_id.startswith("agent:claude-cli:")
    assert "private-claude-session" not in request_id


async def test_claude_cloud_cli_uses_remote_session_environment(tmp_path: Path) -> None:
    server = FakeOpenSAC()
    observation = await agent_cli.run_cli_code(
        "work",
        environ=_env(
            tmp_path,
            CLAUDE_CODE_REMOTE_SESSION_ID="private-claude-cloud-session",
        ),
        transport=httpx.MockTransport(server),
    )

    assert "exit_code=0" in observation
    request_id = server.create_payloads[0]["request_id"]
    assert request_id.startswith("agent:claude-remote-cli:")
    assert "private-claude-cloud-session" not in request_id


async def test_cli_fails_closed_without_context_or_with_ambiguous_hosts(
    tmp_path: Path,
) -> None:
    requests = 0

    def handle(_: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(500)

    missing = await agent_cli.run_cli_code(
        "work",
        environ=_env(tmp_path),
        transport=httpx.MockTransport(handle),
    )
    ambiguous = await agent_cli.run_cli_code(
        "work",
        environ=_env(
            tmp_path,
            CODEX_THREAD_ID="codex",
            CLAUDE_CODE_SESSION_ID="claude",
        ),
        transport=httpx.MockTransport(handle),
    )

    assert "context_unavailable" in missing
    assert "CLAUDE_CODE_REMOTE_SESSION_ID" in missing
    assert "context_ambiguous" in ambiguous
    assert requests == 0


async def test_explicit_context_supports_other_cli_agents(tmp_path: Path) -> None:
    server = FakeOpenSAC()
    observation = await agent_cli.run_cli_code(
        "work",
        environ=_env(
            tmp_path,
            SAC_AGENT_CONTEXT_ID="private-custom-session",
            SAC_AGENT_HOST="custom_agent",
        ),
        transport=httpx.MockTransport(server),
    )

    assert "exit_code=0" in observation
    request_id = server.create_payloads[0]["request_id"]
    assert request_id.startswith("agent:custom_agent:")
    assert "private-custom-session" not in request_id


async def test_explicit_context_overrides_inherited_nested_agent_hosts(tmp_path: Path) -> None:
    server = FakeOpenSAC()
    observation = await agent_cli.run_cli_code(
        "work",
        environ=_env(
            tmp_path,
            SAC_AGENT_CONTEXT_ID="private-child-session",
            SAC_AGENT_HOST="child-agent",
            CODEX_THREAD_ID="inherited-codex-parent",
            CLAUDE_CODE_SESSION_ID="inherited-claude-parent",
        ),
        transport=httpx.MockTransport(server),
    )

    assert "exit_code=0" in observation
    request_id = server.create_payloads[0]["request_id"]
    assert request_id.startswith("agent:child-agent:")
    assert "private-child-session" not in request_id
    assert "inherited-codex-parent" not in request_id
    assert "inherited-claude-parent" not in request_id


async def test_invalid_cli_configuration_fails_before_http(tmp_path: Path) -> None:
    requests = 0

    def handle(_: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(500)

    observation = await agent_cli.run_cli_code(
        "work",
        environ=_env(
            tmp_path,
            CODEX_THREAD_ID="codex",
            SAC_CLI_LEASE_SECONDS="not-a-number",
        ),
        transport=httpx.MockTransport(handle),
    )

    assert "configuration_error" in observation
    assert requests == 0


async def test_invalid_cli_execution_mode_fails_before_http(tmp_path: Path) -> None:
    requests = 0

    def handle(_: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(500)

    observation = await agent_cli.run_cli_code(
        "work",
        environ=_env(
            tmp_path,
            CODEX_THREAD_ID="codex",
            SAC_CLI_EXECUTION_MODE="shared-idle-pool",
        ),
        transport=httpx.MockTransport(handle),
    )

    assert "configuration_error" in observation
    assert "SAC_CLI_EXECUTION_MODE" in observation
    assert requests == 0


async def test_cli_requests_persistent_execution_mode(tmp_path: Path) -> None:
    server = FakeOpenSAC()
    observation = await agent_cli.run_cli_code(
        "value = 1",
        environ=_env(
            tmp_path,
            CODEX_THREAD_ID="repl-thread",
            SAC_CLI_EXECUTION_MODE="persistent_interpreter",
        ),
        transport=httpx.MockTransport(server),
    )

    assert server.create_payloads[0]["execution_mode"] == "persistent_interpreter"
    assert "execution_mode=persistent_interpreter" in observation
    assert "interpreter_state=ready" in observation
    assert "namespace_symbols=3" in observation


async def test_invalid_explicit_host_fails_before_http(tmp_path: Path) -> None:
    observation = await agent_cli.run_cli_code(
        "work",
        environ=_env(
            tmp_path,
            SAC_AGENT_CONTEXT_ID="context",
            SAC_AGENT_HOST="INVALID HOST",
        ),
    )

    assert "context_invalid" in observation


async def test_cli_state_loss_rotates_without_replaying_program(tmp_path: Path) -> None:
    server = FakeOpenSAC()
    env = _env(tmp_path, CODEX_THREAD_ID="thread-loss")
    transport = httpx.MockTransport(server)
    await agent_cli.run_cli_code("setup", environ=env, transport=transport)

    server.next_state_loss = "worker_restarted"
    lost = await agent_cli.run_cli_code("must-not-replay", environ=env, transport=transport)
    recovered = await agent_cli.run_cli_code("fresh", environ=env, transport=transport)

    assert "state_lost" in lost
    assert "exit_code=0" in recovered
    assert [code for _, code in server.exec_calls].count("must-not-replay") == 1
    request_ids = [payload["request_id"] for payload in server.create_payloads]
    assert request_ids[0] == request_ids[1]
    assert request_ids[0].endswith(":g1")
    assert request_ids[2].endswith(":g2")


def test_agent_run_reads_program_from_stdin(monkeypatch) -> None:
    programs: list[str] = []

    async def fake_run_cli_code(code: str, **_kwargs: object) -> str:
        programs.append(code)
        return "[sac_run] ok"

    monkeypatch.setattr(agent_cli, "run_cli_code", fake_run_cli_code)
    result = CliRunner().invoke(cli.app, ["agent-run"], input="print('hello')\n")

    assert result.exit_code == 0
    assert programs == ["print('hello')\n"]
    assert "[sac_run] ok" in result.stdout


def test_cli_and_mcp_adapters_do_not_import_each_other() -> None:
    root = Path(__file__).parents[1] / "src" / "opensac"
    cli_source = (root / "agent" / "cli.py").read_text(encoding="utf-8")
    mcp_source = (root / "agent" / "mcp.py").read_text(encoding="utf-8")

    assert "opensac.agent.mcp" not in cli_source
    assert "opensac.agent.cli" not in mcp_source
