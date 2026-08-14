"""Pure CLI adapter for agent-managed Search-as-Code programs."""

from __future__ import annotations

import asyncio
import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import httpx
import typer

from opensac.agent_session import (
    DEFAULT_LEASE_SECONDS,
    AgentContext,
    AgentSessionConfig,
    AgentSessionManager,
    default_state_dir,
    parse_lease_seconds,
)

_HOST_PATTERN = re.compile(r"^[a-z0-9_-]{1,32}$")
_CONTEXT_UNAVAILABLE = (
    "[sac_run] context_unavailable: No supported agent conversation identifier was "
    "found. Expected CODEX_THREAD_ID, CLAUDE_CODE_SESSION_ID, or "
    "SAC_AGENT_CONTEXT_ID. No OpenSAC session was created."
)


@dataclass(frozen=True)
class CLIConfig(AgentSessionConfig):
    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> CLIConfig:
        env = os.environ if environ is None else environ
        lease_seconds = parse_lease_seconds(
            env.get("SAC_CLI_LEASE_SECONDS", str(DEFAULT_LEASE_SECONDS)),
            "SAC_CLI_LEASE_SECONDS",
        )
        configured_state_dir = env.get("SAC_CLI_STATE_DIR")
        state_dir = (
            Path(configured_state_dir).expanduser()
            if configured_state_dir
            else default_state_dir(env)
        )
        return cls(
            api_base=env.get("SAC_API_BASE", "http://127.0.0.1:8000").rstrip("/"),
            api_key=env.get("SAC_API_KEY") or env.get("OPENSAC_API_KEY") or "",
            lease_seconds=lease_seconds,
            state_dir=state_dir,
        )


def resolve_cli_context(environ: Mapping[str, str] | None = None) -> AgentContext:
    env = os.environ if environ is None else environ
    if explicit_context := env.get("SAC_AGENT_CONTEXT_ID", "").strip():
        host = env.get("SAC_AGENT_HOST", "cli").strip().lower()
        if not _HOST_PATTERN.fullmatch(host):
            raise ValueError(
                "[sac_run] context_invalid: SAC_AGENT_HOST must contain only lowercase "
                "letters, digits, '_' or '-'."
            )
        return AgentContext(host=host, context_id=explicit_context)

    candidates: list[AgentContext] = []
    codex_thread = env.get("CODEX_THREAD_ID", "").strip()
    if codex_thread:
        candidates.append(AgentContext(host="codex-cli", context_id=codex_thread))
    claude_session = env.get("CLAUDE_CODE_SESSION_ID", "").strip()
    claude_remote_session = env.get("CLAUDE_CODE_REMOTE_SESSION_ID", "").strip()
    if claude_session:
        candidates.append(AgentContext(host="claude-cli", context_id=claude_session))
    elif claude_remote_session:
        candidates.append(
            AgentContext(host="claude-remote-cli", context_id=claude_remote_session)
        )

    if not candidates:
        raise ValueError(_CONTEXT_UNAVAILABLE)
    if len(candidates) > 1:
        raise ValueError(
            "[sac_run] context_ambiguous: Multiple agent conversation identifiers were "
            "found. Set SAC_AGENT_CONTEXT_ID and SAC_AGENT_HOST explicitly."
        )
    return candidates[0]


async def run_cli_code(
    code: str,
    *,
    environ: Mapping[str, str] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> str:
    env = os.environ if environ is None else environ
    if not isinstance(code, str) or not code.strip():
        return "[sac_run] Expected a non-empty Python program on stdin or in SOURCE."
    try:
        context = resolve_cli_context(env)
    except ValueError as exc:
        return str(exc)

    try:
        config = CLIConfig.from_env(env)
    except ValueError as exc:
        return f"[sac_run] configuration_error: {exc}"

    manager = AgentSessionManager(
        config, transport=transport, registry_name="cli_sessions.sqlite3"
    )
    try:
        return await manager.run_code(code, context)
    finally:
        await manager.close()


def run_command(source: str) -> None:
    if source == "-":
        if sys.stdin.isatty():
            raise typer.BadParameter("pipe a Python program on stdin or provide SOURCE")
        code = sys.stdin.read()
    else:
        try:
            code = Path(source).read_text(encoding="utf-8")
        except OSError as exc:
            raise typer.BadParameter(f"could not read SOURCE: {exc}") from exc

    observation = asyncio.run(run_cli_code(code))
    typer.echo(observation)
    if "[sac_run] context_" in observation or "configuration_error:" in observation:
        raise typer.Exit(2)
