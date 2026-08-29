"""Shared leased-session policy for local agent adapters."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import sqlite3
import sys
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import httpx

from opensac.agent.sac_run import (
    DEFAULT_TIMEOUT_SECONDS,
    AsyncSessionClient,
    contract_error_code,
    render_observation,
    state_loss_code,
)

DEFAULT_LEASE_SECONDS = 3_600
MAX_LEASE_SECONDS = 86_400
EXECUTION_MODES = frozenset({"program", "persistent_interpreter"})
STATE_LOST_OBSERVATION = (
    "[sac_run] state_lost: The OpenSAC session expired, its worker restarted, or its "
    "persistent interpreter was lost. "
    "The submitted program was not replayed. The next sac_run call will start in a "
    "clean session."
)
EXEC_INDETERMINATE_OBSERVATION = (
    "[sac_run] exec_indeterminate: OpenSAC found an unfinished record for this "
    "execution. Its outcome is unknown, so the program was not replayed."
)
EXEC_ID_CONFLICT_OBSERVATION = (
    "[sac_run] exec_id_conflict: The MCP request identifier was already used for a "
    "different program. The submitted program was not run."
)
EXEC_OUTCOME_UNKNOWN_OBSERVATION = (
    "[sac_run] execution_outcome_unknown: OpenSAC did not return a result after a "
    "same-ID retry. The program may have completed, so it must not be rerun automatically."
)
IDEMPOTENT_EXEC_UNAVAILABLE_OBSERVATION = (
    "[sac_run] idempotent_exec_unavailable: This OpenSAC server does not advertise "
    "safe execution retries. The program was not run."
)
_IDEMPOTENT_EXEC_FEATURE = "idempotent_exec"
_EXEC_TRANSPORT_ATTEMPTS = 2


@dataclass(frozen=True)
class AgentSessionConfig:
    api_base: str
    api_key: str
    lease_seconds: int
    state_dir: Path
    execution_mode: str = "program"


def parse_lease_seconds(raw_value: str, variable_name: str) -> int:
    try:
        lease_seconds = int(raw_value)
    except ValueError as exc:
        raise ValueError(f"{variable_name} must be an integer") from exc
    if not 1 <= lease_seconds <= MAX_LEASE_SECONDS:
        raise ValueError(f"{variable_name} must be between 1 and {MAX_LEASE_SECONDS}")
    return lease_seconds


def parse_execution_mode(raw_value: str, variable_name: str) -> str:
    execution_mode = raw_value.strip()
    if execution_mode not in EXECUTION_MODES:
        allowed = ", ".join(sorted(EXECUTION_MODES))
        raise ValueError(f"{variable_name} must be one of: {allowed}")
    return execution_mode


def default_state_dir(environ: Mapping[str, str]) -> Path:
    if xdg_state_home := environ.get("XDG_STATE_HOME"):
        return Path(xdg_state_home).expanduser() / "opensac"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "OpenSAC"
    if sys.platform == "win32" and (local_app_data := environ.get("LOCALAPPDATA")):
        return Path(local_app_data) / "OpenSAC"
    return Path.home() / ".local" / "state" / "opensac"


class GenerationRegistry:
    """Small process-safe context-hash to generation registry."""

    def __init__(self, state_dir: Path, *, database_name: str = "mcp_sessions.sqlite3") -> None:
        state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            state_dir.chmod(0o700)
        self.path = state_dir / database_name
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


def context_hash(context: AgentContext) -> str:
    namespaced = f"{context.host}\0{context.context_id}".encode()
    return hashlib.sha256(namespaced).hexdigest()


def _policy_hash(config: AgentSessionConfig) -> str:
    policy = json.dumps(
        {
            "api_base": config.api_base,
            "lease_seconds": config.lease_seconds,
            "execution_mode": config.execution_mode,
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
    features: frozenset[str] = frozenset()


class AgentSessionManager:
    """Bind hashed agent contexts to resumable OpenSAC sessions."""

    def __init__(
        self,
        config: AgentSessionConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        registry: GenerationRegistry | None = None,
        registry_name: str = "mcp_sessions.sqlite3",
    ) -> None:
        self.config = config
        self._transport = transport
        self._registry = registry or GenerationRegistry(
            self.config.state_dir, database_name=registry_name
        )
        self._owns_registry = registry is None
        self._policy_hash = _policy_hash(self.config)
        self._entries: dict[str, _SessionEntry] = {}
        self._context_locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()
        self._closed = False

    async def _lock_for(self, hashed_context: str) -> asyncio.Lock:
        async with self._locks_guard:
            return self._context_locks.setdefault(hashed_context, asyncio.Lock())

    def _new_entry(self, host: str, hashed_context: str, generation: int) -> _SessionEntry:
        request_id = f"agent:{host}:{hashed_context}:{self._policy_hash}:g{generation}"
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

    async def _discard_entry(self, hashed_context: str, entry: _SessionEntry) -> None:
        if self._entries.get(hashed_context) is entry:
            self._entries.pop(hashed_context)
        await entry.client.close()

    @staticmethod
    def _exec_id(entry: _SessionEntry, invocation_id: str) -> str:
        value = f"{entry.request_id}\0{invocation_id}".encode()
        return f"mcp:{hashlib.sha256(value).hexdigest()}"

    async def _exec_with_retry(
        self,
        entry: _SessionEntry,
        code: str,
        exec_id: str,
    ) -> dict[str, object]:
        if entry.session_id is None:  # pragma: no cover - guarded by run_code
            raise RuntimeError("OpenSAC session was not created")
        for attempt in range(_EXEC_TRANSPORT_ATTEMPTS):
            try:
                return await entry.client.exec_code(entry.session_id, code, exec_id=exec_id)
            except httpx.TransportError:
                if attempt + 1 == _EXEC_TRANSPORT_ATTEMPTS:
                    raise
        raise RuntimeError("execution retry loop exhausted")  # pragma: no cover

    async def run_code(
        self,
        code: str,
        context: AgentContext,
        *,
        invocation_id: str | None = None,
    ) -> str:
        if not isinstance(code, str) or not code.strip():
            return "[sac_run] Expected a non-empty Python program."
        if self._closed:
            return "[sac_run] Agent session manager is closed."

        hashed_context = context_hash(context)
        context_lock = await self._lock_for(hashed_context)
        async with context_lock:
            generation = self._registry.generation(hashed_context)
            entry = self._entries.get(hashed_context)
            if entry is None or entry.generation != generation:
                if entry is not None:
                    await self._discard_entry(hashed_context, entry)
                entry = self._new_entry(context.host, hashed_context, generation)
                self._entries[hashed_context] = entry

            exec_started = False
            try:
                if entry.session_id is None:
                    session = await entry.client.create_session(
                        {
                            "request_id": entry.request_id,
                            "lease_seconds": self.config.lease_seconds,
                            "execution_mode": self.config.execution_mode,
                        }
                    )
                    entry.session_id = str(session["id"])
                    entry.features = frozenset(str(item) for item in session.get("features", []))

                if invocation_id is None:
                    exec_started = True
                    payload = await entry.client.exec_code(entry.session_id, code)
                else:
                    if _IDEMPOTENT_EXEC_FEATURE not in entry.features:
                        return IDEMPOTENT_EXEC_UNAVAILABLE_OBSERVATION
                    exec_started = True
                    payload = await self._exec_with_retry(
                        entry,
                        code,
                        self._exec_id(entry, invocation_id),
                    )
                observation = render_observation(payload)
                if payload.get("interpreter_state") == "lost":
                    self._registry.advance(hashed_context, generation)
                    with contextlib.suppress(httpx.HTTPError):
                        await entry.client.delete_session(entry.session_id)
                    await self._discard_entry(hashed_context, entry)
                return observation
            except httpx.TimeoutException:
                if invocation_id is not None and exec_started:
                    return EXEC_OUTCOME_UNKNOWN_OBSERVATION
                return f"[sac_run] Timed out after {DEFAULT_TIMEOUT_SECONDS:.0f}s."
            except httpx.HTTPStatusError as exc:
                if state_loss_code(exc.response) is not None:
                    self._registry.advance(hashed_context, generation)
                    await self._discard_entry(hashed_context, entry)
                    return STATE_LOST_OBSERVATION
                error_code = contract_error_code(exc.response)
                if error_code == "exec_indeterminate":
                    return EXEC_INDETERMINATE_OBSERVATION
                if error_code == "exec_id_conflict":
                    return EXEC_ID_CONFLICT_OBSERVATION
                return f"[sac_run] OpenSAC request failed: HTTP {exc.response.status_code}."
            except httpx.TransportError as exc:
                if invocation_id is not None and exec_started:
                    return EXEC_OUTCOME_UNKNOWN_OBSERVATION
                return f"[sac_run] OpenSAC request failed: {type(exc).__name__}."
            except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
                return f"[sac_run] OpenSAC request failed: {type(exc).__name__}."

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        entries = list(self._entries.values())
        self._entries.clear()
        for entry in entries:
            await entry.client.close()
        if self._owns_registry:
            self._registry.close()
