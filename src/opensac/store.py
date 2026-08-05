from __future__ import annotations

import secrets
import shutil
import uuid
from pathlib import Path

from opensac.models import Run, RunCreate, Session, SessionCreate


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
        )
        self.save_session(session)
        return session

    def save_session(self, session: Session) -> None:
        path = self.sessions_dir / session.id / "session.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(session.model_dump_json(indent=2), encoding="utf-8")

    def get_session(self, session_id: str) -> Session:
        path = self.sessions_dir / session_id / "session.json"
        if not path.exists():
            raise KeyError(session_id)
        return Session.model_validate_json(path.read_text(encoding="utf-8"))

    def delete_session(self, session_id: str) -> None:
        self.get_session(session_id)
        shutil.rmtree(self.sessions_dir / session_id)

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

    def artifacts(self, session: Session) -> list[str]:
        workspace = Path(session.workspace)
        return [
            str(path.relative_to(workspace))
            for path in workspace.rglob("*")
            if path.is_file() and not path.name.startswith(".opensac-")
        ]

    def read_artifact(self, session: Session, relative_path: str) -> Path:
        workspace = Path(session.workspace).resolve()
        path = (workspace / relative_path).resolve()
        if not path.is_relative_to(workspace) or not path.is_file():
            raise KeyError(relative_path)
        return path
