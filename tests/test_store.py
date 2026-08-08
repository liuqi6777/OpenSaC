from opensac.models import RunCreate, SessionCreate
from opensac.store import StateStore


def test_store_persists_sessions_runs_and_artifacts(tmp_path) -> None:
    store = StateStore(tmp_path)
    session = store.create_session(SessionCreate(backends=["local"]))
    assert store.get_session(session.id).token == session.token

    run = store.create_run(session.id, RunCreate(input="task"))
    assert store.get_run(run.id).input == "task"

    artifact = tmp_path / "sessions" / session.id / "workspace" / "result.json"
    artifact.write_text("{}")
    assert store.artifacts(session) == ["result.json"]
    assert store.read_artifact(session, "result.json") == artifact

    store.delete_session(session.id)
    assert not artifact.exists()


def test_workspace_snapshot_is_bounded_and_says_what_it_left_out(tmp_path) -> None:
    """Deleting a session is when its evidence disappears.

    The snapshot is what a finished run can be re-questioned from, so it has to
    be bounded -- a workspace holding a corpus dump must not land whole in a
    prediction record -- and it has to say when the bound bit, because a
    snapshot that silently returned half a workspace would misdescribe the
    program that filled it.
    """
    store = StateStore(tmp_path)
    session = store.create_session(SessionCreate(backends=["local"]))
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
    session = store.create_session(SessionCreate(backends=["local"]))
    workspace = tmp_path / "sessions" / session.id / "workspace"
    (workspace / "a.jsonl").write_text("x" * 100)
    (workspace / "b.jsonl").write_text("y" * 100)

    snapshot = store.snapshot_workspace(session, max_total_bytes=50, max_file_bytes=50)

    assert [f.path for f in snapshot.files] == ["a.jsonl"]
    assert snapshot.omitted == ["b.jsonl"]
