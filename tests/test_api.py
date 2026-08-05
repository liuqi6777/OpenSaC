from fastapi.testclient import TestClient

from opensac.api import create_app
from opensac.config import Settings


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
