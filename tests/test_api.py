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
            json={"code": "from opensac_sdk import sdk\n"},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["succeeded"] is True
        assert payload["stdout"] == "ran\n"
        assert payload["output"] == {"records": 3}
        assert payload["citations"][0]["ref"] == "ref_1"
        assert payload["artifacts"] == ["evidence.jsonl"]
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
