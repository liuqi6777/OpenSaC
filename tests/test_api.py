from pathlib import Path

from fastapi.testclient import TestClient

from opensac.api import create_app
from opensac.config import Settings
from opensac.sandbox import SandboxRequest, SandboxResult, UnsafeCodeError


def test_public_session_api_hides_capability_token(tmp_path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        broker_socket=tmp_path / "broker.sock",
        api_key="public-secret",
    )
    with TestClient(create_app(settings)) as client:
        unauthorized = client.post("/v1/sessions", json={})
        assert unauthorized.status_code == 401

        response = client.post(
            "/v1/sessions",
            json={"backends": ["local"]},
            headers={"Authorization": "Bearer public-secret"},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["id"].startswith("sess_")
        assert "token" not in payload
        assert "workspace" not in payload


def test_public_session_api_rejects_unknown_backend(tmp_path) -> None:
    settings = Settings(data_dir=tmp_path / "data", broker_socket=tmp_path / "broker.sock")
    with TestClient(create_app(settings)) as client:
        response = client.post("/v1/sessions", json={"backends": ["unknown"]})
        assert response.status_code == 422


class RecordingSandbox:
    """Stands in for DockerSandbox so /exec is testable without a Docker host."""

    def __init__(self, *, raises: Exception | None = None) -> None:
        self.requests: list[SandboxRequest] = []
        self.raises = raises

    async def execute(self, request: SandboxRequest) -> SandboxResult:
        self.requests.append(request)
        if self.raises is not None:
            raise self.raises
        (request.workspace / "evidence.jsonl").write_text("{}\n", encoding="utf-8")
        return SandboxResult(
            exit_code=0,
            stdout="ran\n",
            stderr="",
            duration_seconds=1.5,
            output={"records": 3},
            citations=[{"ref": "ref_1", "url": "https://example.com"}],
        )


def exec_client(tmp_path, sandbox) -> TestClient:
    settings = Settings(data_dir=tmp_path / "data", broker_socket=tmp_path / "broker.sock")
    app = create_app(settings)
    client = TestClient(app)
    app.state.runtime.sandbox = sandbox
    return client


def test_exec_runs_harness_authored_code_and_reports_artifacts(tmp_path) -> None:
    sandbox = RecordingSandbox()
    with exec_client(tmp_path, sandbox) as client:
        session_id = client.post("/v1/sessions", json={"backends": ["local"]}).json()["id"]
        response = client.post(
            f"/v1/sessions/{session_id}/exec",
            json={"code": "from opensac_sdk import sdk\n", "include_trace": True},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["succeeded"] is True
        assert payload["stdout"] == "ran\n"
        assert payload["output"] == {"records": 3}
        assert payload["citations"][0]["ref"] == "ref_1"
        assert payload["artifacts"] == ["evidence.jsonl"]
        assert payload["trace"] == []
        assert sandbox.requests[0].execution_id
        # No control model was consulted: the caller supplied the program.
        assert sandbox.requests[0].code == "from opensac_sdk import sdk\n"


def test_exec_keeps_one_broker_session_across_turns(tmp_path) -> None:
    """Refs and quota must survive across calls or filesystem serde is useless.

    A program that writes refs to JSONL in turn 1 and resolves them in turn 3
    only works if both turns talk to the same broker session.
    """
    with exec_client(tmp_path, RecordingSandbox()) as client:
        runtime = client.app.state.runtime
        session_id = client.post("/v1/sessions", json={"backends": ["local"]}).json()["id"]
        token = runtime.store.get_session(session_id).token

        client.post(f"/v1/sessions/{session_id}/exec", json={"code": "pass\n"})
        first = runtime.broker.sessions[token]
        first.policy.usage.search_calls = 7

        client.post(f"/v1/sessions/{session_id}/exec", json={"code": "pass\n"})
        assert runtime.broker.sessions[token] is first
        assert client.post(
            f"/v1/sessions/{session_id}/exec", json={"code": "pass\n"}
        ).json()["usage"]["search_calls"] == 7

        client.delete(f"/v1/sessions/{session_id}")
        assert token not in runtime.broker.sessions


def test_exec_returns_validator_rejection_as_an_observation(tmp_path) -> None:
    """The control model has to see why its program was refused and retry."""
    sandbox = RecordingSandbox(raises=UnsafeCodeError("Blocked imports: socket"))
    with exec_client(tmp_path, sandbox) as client:
        session_id = client.post("/v1/sessions", json={"backends": ["local"]}).json()["id"]
        response = client.post(f"/v1/sessions/{session_id}/exec", json={"code": "import socket\n"})
        assert response.status_code == 200
        payload = response.json()
        assert payload["succeeded"] is False
        assert payload["exit_code"] == -1
        assert "Blocked imports: socket" in payload["error"]


def test_exec_rejects_unknown_session(tmp_path) -> None:
    with exec_client(tmp_path, RecordingSandbox()) as client:
        response = client.post("/v1/sessions/sess_missing/exec", json={"code": "pass\n"})
        assert response.status_code == 404


class TurnMarkingSandbox:
    """Writes a file named after the turn, so persistence is observable."""

    def __init__(self) -> None:
        self.requests: list[SandboxRequest] = []

    async def execute(self, request: SandboxRequest) -> SandboxResult:
        turn = len(self.requests)
        self.requests.append(request)
        (request.workspace / f"turn-{turn}.txt").write_text("x", encoding="utf-8")
        return SandboxResult(
            exit_code=0, stdout="", stderr="", duration_seconds=0.5
        )


def test_every_program_is_archived_with_the_name_it_ran_under(tmp_path) -> None:
    """The generated program is the action; a run that keeps only counts has
    thrown away its primary observation, and it cannot be recovered later."""
    sandbox = TurnMarkingSandbox()
    with exec_client(tmp_path, sandbox) as client:
        runtime = client.app.state.runtime
        session_id = client.post("/v1/sessions", json={"backends": ["local"]}).json()["id"]
        for index in range(3):
            client.post(
                f"/v1/sessions/{session_id}/exec",
                json={"code": f"print({index})\n"},
            )

        session = runtime.store.get_session(session_id)
        records = runtime.store.programs(session)
        assert [record.sequence for record in records] == [0, 1, 2]
        assert [record.code for record in records] == ["print(0)\n", "print(1)\n", "print(2)\n"]
        assert records[1].error_category is None

        # The archived file and the file the container ran are named from the
        # same sequence, so "recorded code == executed code" holds structurally
        # rather than by convention.
        assert sandbox.requests[1].program_filename == ".opensac-program-001.py"
        archived = runtime.store.programs_dir(session) / "001.py"
        assert archived.read_text(encoding="utf-8") == "print(1)\n"
        # Archived beside the workspace, never inside it: a program must not be
        # able to read or rewrite the record of what earlier programs did.
        assert runtime.store.programs_dir(session) not in Path(session.workspace).parents


def test_archive_records_a_validator_rejection_too(tmp_path) -> None:
    sandbox = RecordingSandbox(raises=UnsafeCodeError("Blocked imports: socket"))
    with exec_client(tmp_path, sandbox) as client:
        runtime = client.app.state.runtime
        session_id = client.post("/v1/sessions", json={"backends": ["local"]}).json()["id"]
        client.post(f"/v1/sessions/{session_id}/exec", json={"code": "import socket\n"})

        record = runtime.store.programs(runtime.store.get_session(session_id))[0]
        assert record.error_category == "code_validation"
        assert "Blocked imports" in record.error
        assert record.code == "import socket\n"


def test_persistence_disabled_discards_the_workspace_between_calls(tmp_path) -> None:
    """Within one program, write-then-read still works; across calls it does not.

    That is the whole mechanism: what the switch removes is the agent's ability
    to carry its own notes forward, not its ability to use a filesystem.
    """
    with exec_client(tmp_path, TurnMarkingSandbox()) as client:
        session_id = client.post(
            "/v1/sessions",
            json={"backends": ["local"], "mechanisms": {"persistence": False}},
        ).json()["id"]

        first = client.post(f"/v1/sessions/{session_id}/exec", json={"code": "pass\n"})
        second = client.post(f"/v1/sessions/{session_id}/exec", json={"code": "pass\n"})
        assert first.json()["artifacts"] == ["turn-0.txt"]
        assert second.json()["artifacts"] == ["turn-1.txt"]


def test_persistence_enabled_keeps_the_workspace(tmp_path) -> None:
    with exec_client(tmp_path, TurnMarkingSandbox()) as client:
        session_id = client.post("/v1/sessions", json={"backends": ["local"]}).json()["id"]
        client.post(f"/v1/sessions/{session_id}/exec", json={"code": "pass\n"})
        second = client.post(f"/v1/sessions/{session_id}/exec", json={"code": "pass\n"})
        assert sorted(second.json()["artifacts"]) == ["turn-0.txt", "turn-1.txt"]


def test_program_archive_survives_a_session_without_persistence(tmp_path) -> None:
    """The archive lives beside the workspace precisely so this holds."""
    with exec_client(tmp_path, TurnMarkingSandbox()) as client:
        runtime = client.app.state.runtime
        session_id = client.post(
            "/v1/sessions",
            json={"backends": ["local"], "mechanisms": {"persistence": False}},
        ).json()["id"]
        client.post(f"/v1/sessions/{session_id}/exec", json={"code": "print(1)\n"})

        records = runtime.store.programs(runtime.store.get_session(session_id))
        assert [record.code for record in records] == ["print(1)\n"]


def test_session_reports_its_mechanisms_and_reachable_capabilities(tmp_path) -> None:
    """A host builds its skill text from this, not from a copy of the constant.

    Naming a primitive the session cannot reach costs the model a turn to find
    out, so the manifest has to come from the session itself.
    """
    with exec_client(tmp_path, RecordingSandbox()) as client:
        payload = client.post(
            "/v1/sessions",
            json={"backends": ["local"], "mechanisms": {"llm_subroutine": False}},
        ).json()
        assert payload["mechanisms"]["llm_subroutine"] is False
        assert payload["mechanisms"]["batching"] is True
        assert not any(method.startswith("llm.") for method in payload["capabilities"])
        assert "search.local_many" in payload["capabilities"]

        # Recorded on the session, which is what makes an arm recoverable after
        # the run.
        stored = client.app.state.runtime.store.get_session(payload["id"])
        assert stored.mechanisms.llm_subroutine is False


def test_omitted_mechanisms_default_to_the_unablated_session(tmp_path) -> None:
    with exec_client(tmp_path, RecordingSandbox()) as client:
        payload = client.post("/v1/sessions", json={"backends": ["local"]}).json()
        assert payload["mechanisms"] == {
            "batching": True,
            "persistence": True,
            "llm_subroutine": True,
            "context_decoupling": True,
        }
