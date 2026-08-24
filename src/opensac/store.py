from __future__ import annotations

import hashlib
import secrets
import shutil
import stat
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from opensac.models import (
    ExecRecord,
    InterpreterState,
    ProgramRecord,
    RunUsage,
    Session,
    SessionCreate,
    SessionTombstone,
    WorkspaceFile,
    WorkspaceSnapshot,
    utc_now,
)


@dataclass(frozen=True)
class WorkspaceInventory:
    total_bytes: int
    artifacts: list[str]


class StateStore:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir.resolve()
        self.sessions_dir = self.data_dir / "sessions"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

    def create_session(
        self,
        request: SessionCreate,
        *,
        backend: str,
        worker_id: str = "",
        worker_epoch: str = "",
        request_hash: str | None = None,
        default_lease_seconds: float | None = None,
        environment: dict | None = None,
    ) -> Session:
        session_id = f"sess_{uuid.uuid4().hex}"
        workspace = self.sessions_dir / session_id / "workspace"
        workspace.mkdir(parents=True)
        lease_seconds = request.lease_seconds or default_lease_seconds
        created_at = utc_now()
        session = Session(
            id=session_id,
            token=secrets.token_urlsafe(32),
            backends=[backend],
            workspace=str(workspace),
            execution_mode=request.execution_mode,
            interpreter_state=(
                "not_started"
                if request.execution_mode == "persistent_interpreter"
                else "not_applicable"
            ),
            # Frozen onto the session, not read from process settings: the arm a
            # run belongs to has to stay recoverable from its own record after
            # the server has been restarted with a different configuration.
            mechanisms=request.mechanisms,
            request_id=request.request_id,
            request_hash=request_hash,
            worker_id=worker_id,
            worker_epoch=worker_epoch,
            lease_seconds=lease_seconds,
            lease_expires_at=(
                created_at + timedelta(seconds=lease_seconds)
                if lease_seconds is not None
                else None
            ),
            budget=request.budget,
            environment=environment or {},
            created_at=created_at,
            last_access=created_at,
        )
        self.save_session(session)
        return session

    def save_interpreter_state(
        self,
        session_id: str,
        state: InterpreterState,
        *,
        loss_reason: str | None = None,
    ) -> Session:
        session = self.get_session(session_id)
        session.interpreter_state = state
        session.interpreter_loss_reason = loss_reason
        self.save_session(session)
        return session

    def save_session(self, session: Session) -> None:
        path = self.sessions_dir / session.id / "session.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_write_text(path, session.model_dump_json(indent=2))

    @staticmethod
    def _atomic_write_text(path: Path, text: str) -> None:
        """Replace one JSON record without exposing a partial file to retries."""
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(text, encoding="utf-8")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def get_session(self, session_id: str) -> Session:
        path = self.sessions_dir / session_id / "session.json"
        if not path.exists():
            raise KeyError(session_id)
        return Session.model_validate_json(path.read_text(encoding="utf-8"))

    def sessions(self) -> list[Session]:
        """Every durable session, including one left halfway through closing."""
        sessions: list[Session] = []
        for path in sorted(self.sessions_dir.glob("*/session.json")):
            sessions.append(Session.model_validate_json(path.read_text(encoding="utf-8")))
        return sessions

    def find_session_by_request_id(self, request_id: str) -> Session | None:
        for session in self.sessions():
            if session.request_id == request_id:
                return session
        return None

    def touch_session(self, session_id: str, *, at: datetime | None = None) -> Session:
        session = self.get_session(session_id)
        session.last_access = at or utc_now()
        if session.lease_seconds is not None:
            session.lease_expires_at = session.last_access + timedelta(
                seconds=session.lease_seconds
            )
        self.save_session(session)
        return session

    def save_session_usage(
        self,
        session_id: str,
        usage: RunUsage,
        *,
        terminal_reason: str | None = None,
        touch: bool = True,
    ) -> Session:
        session = self.get_session(session_id)
        session.usage = usage
        session.terminal_reason = terminal_reason
        if touch:
            session.last_access = utc_now()
            if session.lease_seconds is not None:
                session.lease_expires_at = session.last_access + timedelta(
                    seconds=session.lease_seconds
                )
        self.save_session(session)
        return session

    def mark_session_closing(self, session_id: str) -> Session:
        session = self.get_session(session_id)
        if not session.closing:
            session.closing = True
            self.save_session(session)
        return session

    def delete_session(self, session_id: str) -> None:
        self.get_session(session_id)
        shutil.rmtree(self.sessions_dir / session_id)

    @property
    def tombstones_dir(self) -> Path:
        path = self.data_dir / "tombstones"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def save_tombstone(self, session: Session, reason: str) -> SessionTombstone:
        tombstone = SessionTombstone(
            session_id=session.id,
            reason=reason,
            request_id=session.request_id,
            request_hash=session.request_hash,
            worker_id=session.worker_id,
            worker_epoch=session.worker_epoch,
        )
        self._atomic_write_text(
            self.tombstones_dir / f"{session.id}.json",
            tombstone.model_dump_json(indent=2),
        )
        return tombstone

    def get_tombstone(self, session_id: str) -> SessionTombstone | None:
        path = self.tombstones_dir / f"{session_id}.json"
        if not path.exists():
            return None
        return SessionTombstone.model_validate_json(path.read_text(encoding="utf-8"))

    def find_tombstone_by_request_id(self, request_id: str) -> SessionTombstone | None:
        for path in self.tombstones_dir.glob("*.json"):
            tombstone = SessionTombstone.model_validate_json(
                path.read_text(encoding="utf-8")
            )
            if tombstone.request_id == request_id:
                return tombstone
        return None

    def reap_tombstones(self, *, before: datetime) -> int:
        removed = 0
        for path in self.tombstones_dir.glob("*.json"):
            try:
                tombstone = SessionTombstone.model_validate_json(
                    path.read_text(encoding="utf-8")
                )
            except (OSError, ValueError):
                continue
            if tombstone.deleted_at <= before:
                path.unlink(missing_ok=True)
                removed += 1
        return removed

    def workspace_bytes(self, session: Session, workspace: Path | None = None) -> int:
        workspace = Path(workspace or session.workspace)
        if not workspace.exists():
            return 0
        return sum(
            path.stat().st_size
            for path in workspace.rglob("*")
            if path.is_file()
        )

    def workspace_inventory(
        self,
        session: Session,
        workspace: Path | None = None,
    ) -> WorkspaceInventory:
        """Collect post-execution byte usage and public artifacts in one walk."""

        workspace = Path(workspace or session.workspace)
        if not workspace.exists():
            return WorkspaceInventory(total_bytes=0, artifacts=[])
        total_bytes = 0
        artifacts: list[str] = []
        for path in workspace.rglob("*"):
            file_stat = path.stat()
            if not stat.S_ISREG(file_stat.st_mode):
                continue
            total_bytes += file_stat.st_size
            if not path.name.startswith(".opensac-"):
                artifacts.append(str(path.relative_to(workspace)))
        return WorkspaceInventory(total_bytes=total_bytes, artifacts=artifacts)

    def execs_dir(self, session: Session) -> Path:
        return self.sessions_dir / session.id / "execs"

    def _exec_record_path(self, session: Session, exec_id: str) -> Path:
        # The client id is data, never a path component. Besides traversal, this
        # also avoids filesystem-specific restrictions on otherwise valid ids.
        digest = hashlib.sha256(exec_id.encode("utf-8")).hexdigest()
        return self.execs_dir(session) / f"{digest}.json"

    def get_exec_record(self, session: Session, exec_id: str) -> ExecRecord | None:
        path = self._exec_record_path(session, exec_id)
        if not path.exists():
            return None
        record = ExecRecord.model_validate_json(path.read_text(encoding="utf-8"))
        if record.exec_id != exec_id:
            raise RuntimeError("Stored execution id does not match its lookup key")
        return record

    def save_exec_record(self, session: Session, record: ExecRecord) -> None:
        path = self._exec_record_path(session, record.exec_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_write_text(path, record.model_dump_json(indent=2))

    def programs_dir(self, session: Session) -> Path:
        """Where generated programs are archived.

        A sibling of the workspace, never inside it. Two consequences are the
        reason: the archive survives a session whose persistence mechanism is
        switched off (the workspace is thrown away between calls there), and it
        is never bind-mounted into the sandbox, so a program cannot read or
        rewrite the record of what earlier programs did.
        """
        return self.sessions_dir / session.id / "programs"

    def reserve_program(self, session: Session, code: str) -> tuple[int, Path]:
        """Claim the next sequence number and write the program under it.

        Callers must hold a per-session lock: two concurrent executions would
        otherwise read the same directory listing and claim the same number.
        """
        directory = self.programs_dir(session)
        directory.mkdir(parents=True, exist_ok=True)
        sequence = len(list(directory.glob("*.py")))
        path = directory / f"{sequence:03d}.py"
        path.write_text(code, encoding="utf-8")
        return sequence, path

    def record_program(self, session: Session, record: ProgramRecord) -> None:
        path = self.programs_dir(session) / "programs.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(record.model_dump_json() + "\n")

    def programs(self, session: Session) -> list[ProgramRecord]:
        path = self.programs_dir(session) / "programs.jsonl"
        if not path.exists():
            return []
        return [
            ProgramRecord.model_validate_json(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def artifacts(self, session: Session, workspace: Path | None = None) -> list[str]:
        """Files the program wrote, as the control model should see them.

        ``workspace`` overrides the session's own directory for a session that
        has persistence disabled and therefore ran against a throwaway one. The
        files are still reported: within a single program, writing and reading
        back is ordinary behaviour, and it is only the survival across calls
        that the switch removes.
        """
        return self.workspace_inventory(session, workspace).artifacts

    def snapshot_workspace(
        self,
        session: Session,
        *,
        max_total_bytes: int,
        max_file_bytes: int,
    ) -> WorkspaceSnapshot:
        """Everything the program wrote, for the record kept after the session dies.

        Deleting a session is the moment its evidence disappears: what the
        rollout actually collected in the sandbox, and what it had decided was
        worth keeping, exist nowhere else. Reading it back first is the
        difference between a run that can be re-questioned and one that can only
        be re-run.

        Bounded, and honest about the bound. A workspace holding a corpus dump
        must not be copied wholesale into a prediction record, but a snapshot
        that quietly returned half of one would misdescribe the program.
        """
        files: list[WorkspaceFile] = []
        omitted: list[str] = []
        used = 0
        for relative in sorted(self.artifacts(session)):
            path = Path(session.workspace) / relative
            try:
                size = path.stat().st_size
            except OSError:
                omitted.append(relative)
                continue
            if used >= max_total_bytes:
                omitted.append(relative)
                continue
            budget = min(max_file_bytes, max_total_bytes - used)
            # errors="replace" rather than a skip: a file that is not valid
            # UTF-8 is still evidence that the program wrote it, and losing the
            # row would be a worse record than losing a few characters.
            text = path.read_bytes()[:budget].decode("utf-8", errors="replace")
            used += len(text)
            files.append(
                WorkspaceFile(
                    path=relative,
                    bytes=size,
                    text=text,
                    truncated=size > budget,
                )
            )
        return WorkspaceSnapshot(files=files, omitted=omitted)
