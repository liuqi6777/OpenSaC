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
