"""Agent-native stdio MCP adapter for persistent OpenSAC sessions."""

from __future__ import annotations

import os
import uuid
from collections.abc import Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

from opensac import _optional
from opensac.agent.session import (
    DEFAULT_LEASE_SECONDS,
    AgentContext,
    AgentSessionConfig,
    AgentSessionManager,
    GenerationRegistry,
    default_state_dir,
    parse_execution_mode,
    parse_lease_seconds,
)

_CONTEXT_UNAVAILABLE_OBSERVATION = (
    "[sac_run] context_unavailable: The MCP host did not provide a Codex thread_id. "
    "No OpenSAC session was created."
)


@dataclass(frozen=True)
class MCPConfig(AgentSessionConfig):
    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> MCPConfig:
        env = os.environ if environ is None else environ
        lease_seconds = parse_lease_seconds(
            env.get("SAC_MCP_LEASE_SECONDS", str(DEFAULT_LEASE_SECONDS)),
            "SAC_MCP_LEASE_SECONDS",
        )
        execution_mode = parse_execution_mode(
            env.get("SAC_MCP_EXECUTION_MODE", "program"),
            "SAC_MCP_EXECUTION_MODE",
        )
        configured_state_dir = env.get("SAC_MCP_STATE_DIR")
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
            execution_mode=execution_mode,
        )


class CodexContextResolver:
    """Compatibility boundary for Codex-specific MCP request metadata."""

    _THREAD_KEYS = ("thread_id", "threadId", "thread-id")
    _NESTED_KEYS = ("codex", "x-codex", "x-codex-turn-metadata")

    def resolve(self, meta: Any) -> AgentContext | None:
        values = _meta_to_mapping(meta)
        context_id = self._find_thread_id(values)
        if context_id is None:
            return None
        return AgentContext(host="codex", context_id=context_id)

    def _find_thread_id(self, values: Mapping[str, Any]) -> str | None:
        for key in self._THREAD_KEYS:
            value = values.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for key in self._NESTED_KEYS:
            nested = values.get(key)
            if isinstance(nested, Mapping):
                value = self._find_thread_id(nested)
                if value is not None:
                    return value
        return None


def _meta_to_mapping(meta: Any) -> Mapping[str, Any]:
    if meta is None:
        return {}
    if isinstance(meta, Mapping):
        return meta
    model_dump = getattr(meta, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump(mode="python", by_alias=True)
        if isinstance(dumped, Mapping):
            return dumped
    return {}


class OpenSACMCP:
    def __init__(
        self,
        config: MCPConfig | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        registry: GenerationRegistry | None = None,
    ) -> None:
        self.config = config or MCPConfig.from_env()
        self._sessions = AgentSessionManager(
            self.config,
            transport=transport,
            registry=registry,
            registry_name="mcp_sessions.sqlite3",
        )
        self._codex = CodexContextResolver()
        self._invocation_namespace = uuid.uuid4().hex
        self._closed = False

    async def run_code(
        self,
        code: str,
        meta: Any = None,
        *,
        invocation_id: str | None = None,
    ) -> str:
        if not isinstance(code, str) or not code.strip():
            return "[sac_run] Expected a non-empty string in the 'code' field."
        if self._closed:
            return "[sac_run] MCP adapter is closed."
        context = self._codex.resolve(meta)
        if context is None:
            return _CONTEXT_UNAVAILABLE_OBSERVATION
        invocation = invocation_id if invocation_id is not None else uuid.uuid4().hex
        namespaced_invocation = f"{self._invocation_namespace}:{invocation}"
        return await self._sessions.run_code(
            code,
            context,
            invocation_id=namespaced_invocation,
        )

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._sessions.close()


def create_server(bridge: OpenSACMCP | None = None) -> FastMCP:
    _optional.require_extra("MCP support", "mcp", ("mcp",))
    from mcp.server.fastmcp import Context, FastMCP

    adapter = bridge or OpenSACMCP()

    @asynccontextmanager
    async def lifespan(_server: Any):
        try:
            yield adapter
        finally:
            await adapter.aclose()

    server = FastMCP(
        "OpenSAC",
        instructions=(
            "Run Search-as-Code programs with sac_run. The current agent conversation is "
            "bound by the MCP host; never create, pass, display, or delete OpenSAC sessions. "
            f"The execution mode is {adapter.config.execution_mode}."
        ),
        lifespan=lifespan,
        log_level="ERROR",
    )

    async def sac_run(code: str, ctx: Any) -> str:
        """Run Python code in this conversation's persistent OpenSAC workspace."""
        return await adapter.run_code(
            code,
            ctx.request_context.meta,
            invocation_id=ctx.request_id,
        )

    sac_run.__annotations__["ctx"] = Context
    server.tool(name="sac_run")(sac_run)
    return server


def run() -> None:
    create_server().run(transport="stdio")


if __name__ == "__main__":
    run()
