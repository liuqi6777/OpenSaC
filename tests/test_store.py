from datetime import UTC, datetime

from opensac.models import ExecRecord, ExecResult, RunUsage, SessionCreate
from opensac.store import StateStore


def test_store_persists_sessions_and_leaves_legacy_runs_untouched(tmp_path) -> None:
    legacy_run = tmp_path / "runs" / "run_old.json"
    legacy_run.parent.mkdir()
    legacy_run.write_text("{}")
    store = StateStore(tmp_path)
    session = store.create_session(SessionCreate(), backend="local")
    assert session.backends == ["local"]
    assert store.get_session(session.id).token == session.token

    artifact = tmp_path / "sessions" / session.id / "workspace" / "result.json"
    artifact.write_text("{}")
    assert store.artifacts(session) == ["result.json"]

    store.delete_session(session.id)
    assert not artifact.exists()
    assert legacy_run.exists()


def test_store_persists_session_lifecycle_and_idempotent_exec_results(tmp_path) -> None:
    store = StateStore(tmp_path)
    assert not (tmp_path / "runs").exists()
    session = store.create_session(SessionCreate(), backend="local")
    touched_at = datetime(2026, 8, 10, 12, tzinfo=UTC)

    touched = store.touch_session(session.id, at=touched_at)
    assert touched.last_access == touched_at
    assert store.mark_session_closing(session.id).closing is True

    record = ExecRecord(
        exec_id="rollout-7:step-3",
        request_hash="a" * 64,
        result=ExecResult(
            exit_code=0,
            stdout="done\n",
            stderr="",
            duration_seconds=1.0,
            succeeded=True,
            usage=RunUsage(search_calls=2),
        ),
    )
    store.save_exec_record(session, record)

    reopened = StateStore(tmp_path)
    restored = reopened.get_exec_record(session, record.exec_id)
    assert restored is not None
    assert restored.request_hash == "a" * 64
    assert restored.result.stdout == "done\n"
    assert restored.result.usage.search_calls == 2
    assert reopened.get_session(session.id).closing is True


def test_workspace_snapshot_is_bounded_and_says_what_it_left_out(tmp_path) -> None:
    """Deleting a session is when its evidence disappears.

    The snapshot is what a finished run can be re-questioned from, so it has to
    be bounded -- a workspace holding a corpus dump must not land whole in a
    prediction record -- and it has to say when the bound bit, because a
    snapshot that silently returned half a workspace would misdescribe the
    program that filled it.
    """
    store = StateStore(tmp_path)
    session = store.create_session(SessionCreate(), backend="local")
    workspace = tmp_path / "sessions" / session.id / "workspace"
    (workspace / "a.jsonl").write_text("x" * 100)
    (workspace / "b.jsonl").write_text("y" * 100)
    (workspace / ".opensac-output.json").write_text("{}")

    snapshot = store.snapshot_workspace(session, max_total_bytes=120, max_file_bytes=80)

    assert [f.path for f in snapshot.files] == ["a.jsonl", "b.jsonl"]
    assert snapshot.files[0].text == "x" * 80
    assert snapshot.files[0].truncated is True
    # The real size is kept even when the text is not, so the record still says
    # how much the program actually wrote.
    assert snapshot.files[0].bytes == 100
    assert snapshot.files[1].text == "y" * 40
    # Runtime internals are excluded, exactly as they are from `artifacts()`.
    assert ".opensac-output.json" not in [f.path for f in snapshot.files]


def test_workspace_snapshot_reports_files_the_budget_never_reached(tmp_path) -> None:
    store = StateStore(tmp_path)
    session = store.create_session(SessionCreate(), backend="local")
    workspace = tmp_path / "sessions" / session.id / "workspace"
    (workspace / "a.jsonl").write_text("x" * 100)
    (workspace / "b.jsonl").write_text("y" * 100)

    snapshot = store.snapshot_workspace(session, max_total_bytes=50, max_file_bytes=50)

    assert [f.path for f in snapshot.files] == ["a.jsonl"]
    assert snapshot.omitted == ["b.jsonl"]
