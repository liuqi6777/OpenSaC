"""Agent-native stdio MCP adapter for persistent OpenSAC sessions."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import sqlite3
import sys
import threading
import time
from collections.abc import Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from mcp.server.fastmcp import Context, FastMCP

from opensac.sac_run import (
    DEFAULT_TIMEOUT_SECONDS,
    AsyncSessionClient,
    render_observation,
    state_loss_code,
)

DEFAULT_LEASE_SECONDS = 3_600
MAX_LEASE_SECONDS = 86_400
_STATE_LOST_OBSERVATION = (
    "[sac_run] state_lost: The OpenSAC session expired or its worker restarted. "
    "The submitted program was not replayed. The next sac_run call will start in a "
    "clean session."
)
_CONTEXT_UNAVAILABLE_OBSERVATION = (
    "[sac_run] context_unavailable: The MCP host did not provide a Codex thread_id "
    "and no Claude Code session was bound. No OpenSAC session was created."
)


@dataclass(frozen=True)
class MCPConfig:
    api_base: str
    api_key: str
    lease_seconds: int
    state_dir: Path

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> MCPConfig:
        env = os.environ if environ is None else environ
        raw_lease = env.get("SAC_MCP_LEASE_SECONDS", str(DEFAULT_LEASE_SECONDS))
        try:
            lease_seconds = int(raw_lease)
        except ValueError as exc:
            raise ValueError("SAC_MCP_LEASE_SECONDS must be an integer") from exc
        if not 1 <= lease_seconds <= MAX_LEASE_SECONDS:
            raise ValueError(
                f"SAC_MCP_LEASE_SECONDS must be between 1 and {MAX_LEASE_SECONDS}"
            )

        configured_state_dir = env.get("SAC_MCP_STATE_DIR")
        state_dir = (
            Path(configured_state_dir).expanduser()
            if configured_state_dir
            else _default_state_dir(env)
        )
        return cls(
            api_base=env.get("SAC_API_BASE", "http://127.0.0.1:8000").rstrip("/"),
            api_key=env.get("SAC_API_KEY") or env.get("OPENSAC_API_KEY") or "",
            lease_seconds=lease_seconds,
            state_dir=state_dir,
        )


def _default_state_dir(environ: Mapping[str, str]) -> Path:
    if xdg_state_home := environ.get("XDG_STATE_HOME"):
        return Path(xdg_state_home).expanduser() / "opensac"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "OpenSAC"
    if sys.platform == "win32" and (local_app_data := environ.get("LOCALAPPDATA")):
        return Path(local_app_data) / "OpenSAC"
    return Path.home() / ".local" / "state" / "opensac"


class GenerationRegistry:
    """Small process-safe context-hash to generation registry."""

    def __init__(self, state_dir: Path) -> None:
        state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            state_dir.chmod(0o700)
        self.path = state_dir / "mcp_sessions.sqlite3"
        self._connection = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        with contextlib.suppress(OSError):
            self.path.chmod(0o600)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA busy_timeout=30000")
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS context_generations (
                context_hash TEXT PRIMARY KEY,
                generation INTEGER NOT NULL CHECK (generation >= 1),
                updated_at REAL NOT NULL
            )
            """
        )
        self._connection.commit()
        self._lock = threading.Lock()

    def generation(self, context_hash: str) -> int:
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT OR IGNORE INTO context_generations
                    (context_hash, generation, updated_at)
                VALUES (?, 1, ?)
                """,
                (context_hash, time.time()),
            )
            row = self._connection.execute(
                "SELECT generation FROM context_generations WHERE context_hash = ?",
                (context_hash,),
            ).fetchone()
        if row is None:  # pragma: no cover - guarded by the insert above
            raise RuntimeError("generation registry insert failed")
        return int(row[0])

    def advance(self, context_hash: str, stale_generation: int) -> int:
        """Advance once; concurrent observers of the same loss converge."""
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT OR IGNORE INTO context_generations
                    (context_hash, generation, updated_at)
                VALUES (?, ?, ?)
                """,
                (context_hash, stale_generation, time.time()),
            )
            self._connection.execute(
                """
                UPDATE context_generations
                SET generation = generation + 1, updated_at = ?
                WHERE context_hash = ? AND generation = ?
                """,
                (time.time(), context_hash, stale_generation),
            )
            row = self._connection.execute(
                "SELECT generation FROM context_generations WHERE context_hash = ?",
                (context_hash,),
            ).fetchone()
        if row is None:  # pragma: no cover - guarded by the insert above
            raise RuntimeError("generation registry update failed")
        return int(row[0])

    def close(self) -> None:
        with self._lock:
            self._connection.close()


@dataclass(frozen=True)
class AgentContext:
    host: str
    context_id: str


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


def _context_hash(context: AgentContext) -> str:
    namespaced = f"{context.host}\0{context.context_id}".encode()
    return hashlib.sha256(namespaced).hexdigest()


def _policy_hash(config: MCPConfig) -> str:
    policy = json.dumps(
        {
            "api_base": config.api_base,
            "lease_seconds": config.lease_seconds,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(policy.encode()).hexdigest()


@dataclass
class _SessionEntry:
    generation: int
    request_id: str
    client: AsyncSessionClient
    session_id: str | None = None


class OpenSACMCP:
    def __init__(
        self,
        config: MCPConfig | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        registry: GenerationRegistry | None = None,
    ) -> None:
        self.config = config or MCPConfig.from_env()
        self._transport = transport
        self._registry = registry or GenerationRegistry(self.config.state_dir)
        self._owns_registry = registry is None
        self._policy_hash = _policy_hash(self.config)
        self._codex = CodexContextResolver()
        self._claude_context_id: str | None = None
        self._entries: dict[str, _SessionEntry] = {}
        self._context_locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()
        self._closed = False

    def bind_context(self, context_id: str) -> str:
        if not isinstance(context_id, str) or not context_id.strip():
            return "context_unavailable: context_id must be a non-empty string"
        self._claude_context_id = context_id.strip()
        return "Claude Code session context bound"

    def _resolve_context(self, meta: Any) -> AgentContext | None:
        codex = self._codex.resolve(meta)
        if codex is not None:
            return codex
        if self._claude_context_id is not None:
            return AgentContext(host="claude", context_id=self._claude_context_id)
        return None

    async def _lock_for(self, context_hash: str) -> asyncio.Lock:
        async with self._locks_guard:
            return self._context_locks.setdefault(context_hash, asyncio.Lock())

    def _new_entry(
        self, host: str, context_hash: str, generation: int
    ) -> _SessionEntry:
        request_id = (
            f"agent:{host}:{context_hash}:{self._policy_hash}:g{generation}"
        )
        return _SessionEntry(
            generation=generation,
            request_id=request_id,
            client=AsyncSessionClient(
                api_base=self.config.api_base,
                api_key=self.config.api_key,
                timeout=DEFAULT_TIMEOUT_SECONDS,
                transport=self._transport,
            ),
        )

    async def _discard_entry(self, context_hash: str, entry: _SessionEntry) -> None:
        if self._entries.get(context_hash) is entry:
            self._entries.pop(context_hash)
        await entry.client.close()

    async def run_code(self, code: str, meta: Any = None) -> str:
        if not isinstance(code, str) or not code.strip():
            return "[sac_run] Expected a non-empty string in the 'code' field."
        if self._closed:
            return "[sac_run] MCP adapter is closed."

        context = self._resolve_context(meta)
        if context is None:
            return _CONTEXT_UNAVAILABLE_OBSERVATION
        context_hash = _context_hash(context)
        context_lock = await self._lock_for(context_hash)

        async with context_lock:
            generation = self._registry.generation(context_hash)
            entry = self._entries.get(context_hash)
            if entry is None or entry.generation != generation:
                if entry is not None:
                    await self._discard_entry(context_hash, entry)
                entry = self._new_entry(context.host, context_hash, generation)
                self._entries[context_hash] = entry

            try:
                if entry.session_id is None:
                    session = await entry.client.create_session(
                        {
                            "request_id": entry.request_id,
                            "lease_seconds": self.config.lease_seconds,
                        }
                    )
                    entry.session_id = str(session["id"])
                payload = await entry.client.exec_code(entry.session_id, code)
                return render_observation(payload)
            except httpx.TimeoutException:
                return f"[sac_run] Timed out after {DEFAULT_TIMEOUT_SECONDS:.0f}s."
            except httpx.HTTPStatusError as exc:
                if state_loss_code(exc.response) is not None:
                    self._registry.advance(context_hash, generation)
                    await self._discard_entry(context_hash, entry)
                    return _STATE_LOST_OBSERVATION
                return f"[sac_run] OpenSAC request failed: HTTP {exc.response.status_code}."
            except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
                return f"[sac_run] OpenSAC request failed: {type(exc).__name__}."

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        entries = list(self._entries.values())
        self._entries.clear()
        for entry in entries:
            await entry.client.close()
        if self._owns_registry:
            self._registry.close()


def create_server(bridge: OpenSACMCP | None = None) -> FastMCP:
    adapter = bridge or OpenSACMCP()

    @asynccontextmanager
    async def lifespan(_server: FastMCP):
        try:
            yield adapter
        finally:
            await adapter.aclose()

    server = FastMCP(
        "OpenSAC",
        instructions=(
            "Run Search-as-Code programs with sac_run. The current agent conversation is "
            "bound by the MCP host; never create, pass, display, or delete OpenSAC sessions. "
            "The bind_context tool is reserved for the Claude Code host hook."
        ),
        lifespan=lifespan,
        log_level="ERROR",
    )

    @server.tool(name="sac_run")
    async def sac_run(code: str, ctx: Context) -> str:
        """Run Python code in this conversation's persistent OpenSAC workspace."""
        return await adapter.run_code(code, ctx.request_context.meta)

    @server.tool(name="bind_context")
    async def bind_context(context_id: str) -> str:
        """INTERNAL: bind the Claude Code session id supplied by a host hook."""
        return adapter.bind_context(context_id)

    return server


def run() -> None:
    create_server().run(transport="stdio")


if __name__ == "__main__":
    run()
