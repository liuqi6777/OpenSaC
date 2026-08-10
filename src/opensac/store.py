from __future__ import annotations

import hashlib
import secrets
import shutil
import uuid
from datetime import datetime
from pathlib import Path

from opensac.models import (
    ExecRecord,
    ProgramRecord,
    Run,
    RunCreate,
    Session,
    SessionCreate,
    WorkspaceFile,
    WorkspaceSnapshot,
    utc_now,
)


class StateStore:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir.resolve()
        self.sessions_dir = self.data_dir / "sessions"
        self.runs_dir = self.data_dir / "runs"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.runs_dir.mkdir(parents=True, exist_ok=True)

    def create_session(self, request: SessionCreate) -> Session:
        session_id = f"sess_{uuid.uuid4().hex}"
        workspace = self.sessions_dir / session_id / "workspace"
        workspace.mkdir(parents=True)
        session = Session(
            id=session_id,
            token=secrets.token_urlsafe(32),
            backends=request.backends,
            limits=request.limits,
            workspace=str(workspace),
            # Frozen onto the session, not read from process settings: the arm a
            # run belongs to has to stay recoverable from its own record after
            # the server has been restarted with a different configuration.
            mechanisms=request.mechanisms,
        )
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

    def touch_session(self, session_id: str, *, at: datetime | None = None) -> Session:
        session = self.get_session(session_id)
        session.last_access = at or utc_now()
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

    def create_run(self, session_id: str, request: RunCreate) -> Run:
        run = Run(
            id=f"run_{uuid.uuid4().hex}",
            session_id=session_id,
            input=request.input,
            model=request.model,
            output_schema=request.output_schema,
            include_trace=request.include_trace,
        )
        self.save_run(run)
        return run

    def save_run(self, run: Run) -> None:
        path = self.runs_dir / f"{run.id}.json"
        path.write_text(run.model_dump_json(indent=2), encoding="utf-8")

    def get_run(self, run_id: str) -> Run:
        path = self.runs_dir / f"{run_id}.json"
        if not path.exists():
            raise KeyError(run_id)
        return Run.model_validate_json(path.read_text(encoding="utf-8"))

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
        workspace = Path(workspace or session.workspace)
        if not workspace.exists():
            return []
        return [
            str(path.relative_to(workspace))
            for path in workspace.rglob("*")
            if path.is_file() and not path.name.startswith(".opensac-")
        ]

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

    def read_artifact(self, session: Session, relative_path: str) -> Path:
        workspace = Path(session.workspace).resolve()
        path = (workspace / relative_path).resolve()
        if not path.is_relative_to(workspace) or not path.is_file():
            raise KeyError(relative_path)
        return path
