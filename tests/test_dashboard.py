from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from opensac._contracts import ContentSnippet, SearchHit
from opensac.api import create_app
from opensac.api.dashboard import DashboardTelemetry, dashboard_event_stream
from opensac.broker.service import BrokerService
from opensac.config import Settings
from opensac.models import CapabilityEvent, ExecResult, RunUsage, Session


def _settings(tmp_path: Path, **overrides) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        broker_socket=tmp_path / "broker.sock",
        **overrides,
    )


def test_dashboard_routes_serve_packaged_assets_and_snapshot(tmp_path: Path) -> None:
    with TestClient(create_app(_settings(tmp_path))) as client:
        page = client.get("/dashboard")
        script = client.get("/dashboard/assets/app.js")
        styles = client.get("/dashboard/assets/styles.css")
        snapshot = client.get("/dashboard/api/snapshot")

    assert page.status_code == 200
    assert "OpenSAC Runtime" in page.text
    assert "default-src 'none'" in page.headers["content-security-policy"]
    assert page.headers["x-content-type-options"] == "nosniff"
    assert script.status_code == 200
    assert '"use strict"' in script.text
    assert styles.status_code == 200
    assert "--accent:" in styles.text
    assert snapshot.status_code == 200
    assert snapshot.json()["version"] == 1
    assert snapshot.json()["health"]["status"] == "ok"


def test_dashboard_debug_apis_reuse_bearer_authentication(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        api_host="0.0.0.0",
        api_key="dashboard-secret",
        dashboard_enabled=True,
    )
    with TestClient(create_app(settings)) as client:
        assert client.get("/dashboard").status_code == 200
        assert client.get("/dashboard/api/snapshot").status_code == 401
        authorized = client.get(
            "/dashboard/api/snapshot",
            headers={"Authorization": "Bearer dashboard-secret"},
        )

    assert authorized.status_code == 200
    assert "dashboard-secret" not in authorized.text


def test_dashboard_routes_are_absent_when_disabled(tmp_path: Path) -> None:
    settings = _settings(tmp_path, api_host="0.0.0.0")
    assert settings.dashboard_is_enabled is False

    with TestClient(create_app(settings)) as client:
        assert client.get("/dashboard").status_code == 404
        assert client.get("/dashboard/api/snapshot").status_code == 404


def test_telemetry_redacts_bounds_and_cleans_up_execution_state() -> None:
    secret = "provider-secret-value"
    telemetry = DashboardTelemetry(enabled=True, secrets=[secret])
    queue = telemetry.subscribe()
    task_id = telemetry.start_execution(
        session_id="sess_public",
        exec_id="exec_public",
        execution_mode="program",
        code=f"token = '{secret}'\n" + "x" * (70 * 1024),
    )
    assert task_id is not None
    telemetry.set_phase(task_id, "preparing")
    telemetry.bind_execution(task_id, "internal-exec")
    telemetry.capability_started(
        "internal-exec",
        1,
        "search.query",
        {"query": f"find {secret}"},
    )
    telemetry.capability_completed(
        "internal-exec",
        1,
        CapabilityEvent(
            sequence=1,
            method="search.query",
            status="ok",
            duration_seconds=0.2,
            queries=[f"find {secret}"],
            input_count=1,
            result_count=1,
        ),
        {"hits": [{"snippet": secret}]},
    )

    active_snapshot = telemetry.snapshot({"status": "ok"})
    serialized_active = json.dumps(active_snapshot)
    assert secret not in serialized_active
    assert active_snapshot["executions"][0]["code"]["truncated"] is True
    assert active_snapshot["executions"][0]["phase"] == "preparing"
    assert active_snapshot["executions"][0]["capabilities"][0]["status"] == "ok"

    result = ExecResult(
        exec_id="exec_public",
        exit_code=0,
        stdout=f"done {secret}",
        stderr="",
        duration_seconds=0.3,
        succeeded=True,
        output={"value": secret},
        usage=RunUsage(exec_calls=1, search_calls=1),
    )
    telemetry.complete_execution(task_id, result=result)
    completed_snapshot = telemetry.snapshot({"status": "ok"})

    assert completed_snapshot["executions"] == []
    assert completed_snapshot["counters"] == {
        "started": 1,
        "completed": 1,
        "succeeded": 1,
        "failed": 0,
        "cancelled": 0,
        "timed_out": 0,
        "output_limit_exceeded": 0,
    }
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    assert events[-1]["type"] == "exec.completed"
    assert secret not in json.dumps(events)
    telemetry.unsubscribe(queue)


@pytest.mark.asyncio
async def test_broker_publishes_capability_lifecycle_to_dashboard() -> None:
    class Backend:
        name = "local"
        provider_identity = "dashboard-test"
        supports_domains = False
        max_depth = 10

        async def search(self, query, *, limit, offset=0, domains=None):
            del domains
            return [
                SearchHit(
                    source="",
                    backend="local",
                    title=query,
                    docid=str(offset + 1),
                    snippet="result",
                    rank=offset + 1,
                )
            ][:limit]

        async def fetch(self, hit, *, query=None):
            del query
            return ContentSnippet(source=hit.source, text="document")

    telemetry = DashboardTelemetry(enabled=True, secrets=[])
    queue = telemetry.subscribe()
    task_id = telemetry.start_execution(
        session_id="sess_dashboard",
        exec_id="exec_dashboard",
        execution_mode="program",
        code="pass",
    )
    assert task_id is not None
    telemetry.bind_execution(task_id, "internal-dashboard-exec")
    service = BrokerService(
        {"local": Backend()},
        capability_observer=telemetry,
    )
    service.register_session(
        Session(
            id="sess_dashboard",
            token="token",
            backends=["local"],
            workspace="/tmp/dashboard-test",
        )
    )

    result = await service.call(
        "token",
        "search.query",
        {"query": "dashboard", "limit": 1},
        execution_id="internal-dashboard-exec",
    )

    assert len(result) == 1
    event_types = []
    while not queue.empty():
        event_types.append(queue.get_nowait()["type"])
    assert event_types == [
        "exec.started",
        "capability.started",
        "capability.completed",
    ]
    capability = telemetry.snapshot({"status": "ok"})["executions"][0]["capabilities"][0]
    assert capability["method"] == "search.query"
    assert capability["status"] == "ok"
    await service.aclose()
    telemetry.unsubscribe(queue)


@pytest.mark.asyncio
async def test_event_stream_starts_with_snapshot_and_releases_subscriber() -> None:
    telemetry = DashboardTelemetry(enabled=True, secrets=[])

    class Runtime:
        dashboard = telemetry

        @staticmethod
        def dashboard_snapshot() -> dict[str, object]:
            return {"version": 1, "health": {"status": "ok"}}

    request = Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/dashboard/api/events",
            "raw_path": b"/dashboard/api/events",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "server": ("127.0.0.1", 8000),
            "root_path": "",
        }
    )
    stream = dashboard_event_stream(Runtime(), request)  # type: ignore[arg-type]

    first = (await anext(stream)).decode()
    await stream.aclose()

    assert "event: snapshot" in first
    assert '"status":"ok"' in first
    assert telemetry._subscribers == set()


def test_dashboard_sources_are_mapped_into_the_wheel() -> None:
    root = Path(__file__).resolve().parents[1]
    metadata = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    assets = root / "dashboard"

    assert metadata["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"] == {
        "dashboard": "opensac/_dashboard_assets"
    }
    assert {path.name for path in assets.iterdir() if path.is_file()} == {
        "index.html",
        "app.js",
        "styles.css",
    }
