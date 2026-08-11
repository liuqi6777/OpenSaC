import asyncio
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from opensac.api import create_app
from opensac.api.app import (
    ApplicationRuntime,
    ExecIndeterminateError,
    SessionCleanupError,
    SessionClosingError,
    SessionExpiredError,
)
from opensac.config import Settings
from opensac.models import (
    ExecCreate,
    ExecRecord,
    ExecRecordStatus,
    SessionCreate,
)
from opensac.sandbox import SandboxRequest, SandboxResult, UnsafeCodeError, WarmDockerSandbox


def test_public_session_api_hides_capability_token(tmp_path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        broker_socket=tmp_path / "broker.sock",
        api_key="public-secret",
        backend_metadata_hash="sha256:index-manifest",
        extract_max_items=12,
        citation_max_evidence_chars=4096,
    )
    with TestClient(create_app(settings)) as client:
        unauthorized = client.post("/v1/sessions", json={})
        assert unauthorized.status_code == 401

        response = client.post(
            "/v1/sessions",
            json={"backends": ["local"], "limits": {"max_turns": 1}},
            headers={"Authorization": "Bearer public-secret"},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["id"].startswith("sess_")
        assert "token" not in payload
        assert "workspace" not in payload
        assert "limits" not in payload
        assert set(payload["features"]) == {
            "capability_contract_v1",
            "idempotent_exec",
            "worker_affinity",
            "idempotent_session_create",
            "leases",
            "resource_budgets",
            "abort_session",
        }
        assert payload["worker_id"]
        assert payload["worker_epoch"]
        assert response.headers["X-OpenSAC-Worker-ID"] == payload["worker_id"]
        assert payload["closing"] is False
        assert payload["last_access"]
        assert payload["environment"]["backend_metadata_hash"] == "sha256:index-manifest"
        assert payload["environment"]["sandbox_contract"] == 3
        assert payload["environment"]["capability_contract"] == 1
        capability_limits = payload["environment"]["capability_limits"]
        assert capability_limits["extract_many"]["max_items"] == 12
        assert capability_limits["evidence"]["max_chars"] == 4096
        assert not any(method.startswith("llm.") for method in payload["capabilities"])


def test_public_session_api_rejects_unknown_backend(tmp_path) -> None:
    settings = Settings(data_dir=tmp_path / "data", broker_socket=tmp_path / "broker.sock")
    with TestClient(create_app(settings)) as client:
        response = client.post("/v1/sessions", json={"backends": ["unknown"]})
        assert response.status_code == 422


def test_openapi_exposes_exec_but_no_internal_run_routes(tmp_path) -> None:
    settings = Settings(data_dir=tmp_path / "data", broker_socket=tmp_path / "broker.sock")
    with TestClient(create_app(settings)) as client:
        schema = client.get("/openapi.json").json()
        paths = schema["paths"]

    assert schema["info"]["version"] == "0.2.0"
    assert "/v1/sessions/{session_id}/exec" in paths
    assert all("/runs" not in path for path in paths)


def test_a_session_takes_exactly_one_search_backend(tmp_path) -> None:
    """Two would leave `search.query` picking one and the program unable to tell.

    Refused at creation rather than resolved at call time: a session that
    searched half of what it enabled is wrong in a way nothing downstream can
    detect. Mixed retrieval, when an experiment wants it, is an explicit
    parameter and an arm of its own.
    """
    settings = Settings(data_dir=tmp_path / "data", broker_socket=tmp_path / "broker.sock")
    with TestClient(create_app(settings)) as client:
        both = client.post("/v1/sessions", json={"backends": ["web", "local"]})
        assert both.status_code == 422
        assert "exactly one search backend" in both.json()["detail"]
        assert client.post("/v1/sessions", json={"backends": []}).status_code == 422
        # The default is one backend, so an omitted field stays valid.
        assert client.post("/v1/sessions", json={}).status_code == 200


def test_session_create_is_idempotent_and_capacity_is_admitted_up_front(tmp_path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        broker_socket=tmp_path / "broker.sock",
        max_active_sessions=1,
    )
    with TestClient(create_app(settings)) as client:
        request = {
            "request_id": "rollout-7:attempt-1",
            "lease_seconds": 60,
            "budget": {"max_exec_calls": 2},
        }
        first = client.post("/v1/sessions", json=request)
        retry = client.post("/v1/sessions", json=request)
        conflict = client.post(
            "/v1/sessions",
            json={**request, "budget": {"max_exec_calls": 3}},
        )
        full = client.post(
            "/v1/sessions",
            json={"request_id": "rollout-8:attempt-1"},
        )

        assert first.status_code == retry.status_code == 200
        assert first.json()["id"] == retry.json()["id"]
        assert conflict.status_code == 409
        assert conflict.json()["detail"]["code"] == "session_request_conflict"
        assert full.status_code == 429
        assert full.json()["detail"] == {
            "code": "capacity_exhausted",
            "message": "Worker session capacity 1 is full",
            "retryable": True,
        }
        assert full.headers["Retry-After"] == "1"


@pytest.mark.asyncio
async def test_concurrent_session_create_request_id_produces_one_directory(tmp_path) -> None:
    runtime = ApplicationRuntime(
        Settings(data_dir=tmp_path / "data", broker_socket=tmp_path / "broker.sock")
    )
    request = SessionCreate(request_id="same-rollout", backends=["local"])
    try:
        results = await asyncio.gather(
            *(runtime.create_session(request) for _ in range(20))
        )
        assert {session.id for session, _ in results} == {results[0][0].id}
        assert sum(created for _, created in results) == 1
        assert len(runtime.store.sessions()) == 1
    finally:
        await runtime.model_client.close()


def test_heartbeat_renews_lease_and_drain_rejects_only_new_sessions(tmp_path) -> None:
    settings = Settings(data_dir=tmp_path / "data", broker_socket=tmp_path / "broker.sock")
    with TestClient(create_app(settings)) as client:
        created = client.post(
            "/v1/sessions",
            json={"request_id": "leased", "lease_seconds": 30},
        ).json()
        time.sleep(0.01)
        heartbeat = client.post(f"/v1/sessions/{created['id']}/heartbeat")
        assert heartbeat.status_code == 200
        assert heartbeat.json()["lease_expires_at"] > created["lease_expires_at"]

        drained = client.post("/v1/admin/drain")
        assert drained.json()["status"] == "draining"
        assert client.get("/healthz").json()["accepting"] is False
        assert client.get(f"/v1/sessions/{created['id']}").status_code == 200
        rejected = client.post("/v1/sessions", json={"request_id": "new"})
        assert rejected.status_code == 503
        assert rejected.json()["detail"]["code"] == "worker_draining"


def test_worker_restart_invalidates_old_epoch_sessions(tmp_path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        broker_socket=tmp_path / "broker.sock",
        worker_id="worker-a",
    )
    with TestClient(create_app(settings)) as client:
        created = client.post(
            "/v1/sessions", json={"request_id": "restart-me"}
        ).json()
        old_epoch = created["worker_epoch"]

    with TestClient(create_app(settings)) as restarted:
        health = restarted.get("/healthz").json()
        response = restarted.get(f"/v1/sessions/{created['id']}")
        create_retry = restarted.post(
            "/v1/sessions", json={"request_id": "restart-me"}
        )
        create_conflict = restarted.post(
            "/v1/sessions",
            json={"request_id": "restart-me", "lease_seconds": 60},
        )

    assert health["worker_id"] == "worker-a"
    assert health["worker_epoch"] != old_epoch
    assert response.status_code == 410
    assert response.json()["detail"]["code"] == "worker_restarted"
    assert create_retry.status_code == 410
    assert create_retry.json()["detail"]["code"] == "worker_restarted"
    assert create_conflict.status_code == 409
    assert create_conflict.json()["detail"]["code"] == "session_request_conflict"


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


class BlockingSandbox(RecordingSandbox):
    """Holds one execution open so DELETE and duplicate requests can race it."""

    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.close_calls: list[str] = []

    async def execute(self, request: SandboxRequest) -> SandboxResult:
        self.requests.append(request)
        self.started.set()
        await self.release.wait()
        return SandboxResult(
            exit_code=0,
            stdout="ran once\n",
            stderr="",
            duration_seconds=1.0,
            output={"request": len(self.requests)},
        )

    async def close_session(self, session) -> None:
        self.close_calls.append(session.id)


class RetryingCloseSandbox(RecordingSandbox):
    def __init__(self, *, fail: bool = True) -> None:
        super().__init__()
        self.fail = fail
        self.close_calls: list[str] = []

    async def close_session(self, session) -> None:
        self.close_calls.append(session.id)
        if self.fail:
            raise RuntimeError("docker rm failed")


class BlockingCloseSandbox(RecordingSandbox):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.close_calls: list[str] = []

    async def close_session(self, session) -> None:
        self.close_calls.append(session.id)
        self.started.set()
        await self.release.wait()


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
        assert set(payload["timings"]) >= {
            "session_queue_seconds",
            "prepare_seconds",
            "sandbox_queue_seconds",
            "sandbox_execute_seconds",
            "postprocess_seconds",
            "server_total_seconds",
        }
        assert sandbox.requests[0].execution_id
        # No control model was consulted: the caller supplied the program.
        assert sandbox.requests[0].code == "from opensac_sdk import sdk\n"


def test_health_reports_capacity_and_warm_mode_is_selectable(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def reap_orphans(_: WarmDockerSandbox) -> int:
        return 0

    monkeypatch.setattr(WarmDockerSandbox, "reap_orphans", reap_orphans)
    settings = Settings(
        data_dir=tmp_path / "data",
        broker_socket=tmp_path / "broker.sock",
        sandbox_mode="warm",
        sandbox_max_concurrency=3,
    )
    app = create_app(settings)
    assert isinstance(app.state.runtime.sandbox, WarmDockerSandbox)
    with TestClient(app) as client:
        payload = client.get("/healthz").json()
    assert payload["sandbox_mode"] == "warm"
    assert payload["sandbox"] == {
        "capacity": 3,
        "active": 0,
        "waiting": 0,
        "admitted": 0,
    }
    assert payload["broker"]["capacity"] == settings.max_concurrency
    assert payload["warm"]["limit"] == 0
    assert payload["warm"]["capacity"] == 0
    assert payload["warm"]["active"] == 0
    assert payload["warm"]["waiting"] == 0
    assert payload["sessions"]["waiting"] == 0
    assert payload["state"] == "accepting"


def test_exec_id_retries_return_the_persisted_result_and_conflicts_are_409(tmp_path) -> None:
    sandbox = RecordingSandbox()
    with exec_client(tmp_path, sandbox) as client:
        session_id = client.post("/v1/sessions", json={"backends": ["local"]}).json()["id"]
        request = {"exec_id": "rollout-1:turn-4", "code": "print('once')\n"}

        first = client.post(f"/v1/sessions/{session_id}/exec", json=request)
        retry = client.post(f"/v1/sessions/{session_id}/exec", json=request)
        conflict = client.post(
            f"/v1/sessions/{session_id}/exec",
            json={"exec_id": request["exec_id"], "code": "print('different')\n"},
        )

        assert first.status_code == retry.status_code == 200
        assert retry.json() == first.json()
        assert len(sandbox.requests) == 1
        assert conflict.status_code == 409
        assert conflict.json()["detail"]["code"] == "exec_id_conflict"
        assert "different payload" in conflict.json()["detail"]["message"]


def test_exec_budget_is_hard_and_idempotent_replay_is_not_charged(tmp_path) -> None:
    sandbox = RecordingSandbox()
    with exec_client(tmp_path, sandbox) as client:
        session = client.post(
            "/v1/sessions",
            json={
                "request_id": "budgeted-rollout",
                "budget": {"max_exec_calls": 1},
            },
        ).json()
        request = {"exec_id": "turn-1", "code": "pass\n"}
        first = client.post(f"/v1/sessions/{session['id']}/exec", json=request)
        replay = client.post(f"/v1/sessions/{session['id']}/exec", json=request)
        blocked = client.post(
            f"/v1/sessions/{session['id']}/exec",
            json={"exec_id": "turn-2", "code": "pass\n"},
        )

        assert first.status_code == replay.status_code == 200
        assert replay.json() == first.json()
        assert first.json()["usage"]["exec_calls"] == 1
        assert first.json()["session_state"] == "exhausted"
        assert first.json()["budget_remaining"]["max_exec_calls"] == 0
        assert blocked.status_code == 409
        assert blocked.json()["detail"]["code"] == "budget_exhausted"
        assert len(sandbox.requests) == 1


def test_workspace_and_sandbox_budgets_are_reported_on_the_exec_result(tmp_path) -> None:
    sandbox = RecordingSandbox()
    with exec_client(tmp_path, sandbox) as client:
        session = client.post(
            "/v1/sessions",
            json={
                "budget": {
                    "max_sandbox_seconds": 0.5,
                    "max_workspace_bytes": 1,
                }
            },
        ).json()
        response = client.post(
            f"/v1/sessions/{session['id']}/exec",
            json={"code": "pass\n"},
        )

        assert response.status_code == 200
        assert sandbox.requests[0].timeout_seconds == 0.5
        assert response.json()["usage"]["workspace_bytes"] == 3
        assert response.json()["session_state"] == "exhausted"
        assert response.json()["terminal_reason"] == (
            "budget_exhausted:max_workspace_bytes"
        )


@pytest.mark.asyncio
async def test_concurrent_identical_exec_ids_run_the_sandbox_once(tmp_path) -> None:
    runtime = ApplicationRuntime(
        Settings(data_dir=tmp_path / "data", broker_socket=tmp_path / "broker.sock")
    )
    sandbox = BlockingSandbox()
    runtime.sandbox = sandbox
    session = runtime.store.create_session(SessionCreate(backends=["local"]))
    request = ExecCreate(exec_id="rollout-2:turn-9", code="print('once')\n")
    try:
        first = asyncio.create_task(runtime.execute_code(session.id, request))
        await asyncio.wait_for(sandbox.started.wait(), timeout=1)
        duplicate = asyncio.create_task(runtime.execute_code(session.id, request))
        await asyncio.sleep(0)
        assert len(sandbox.requests) == 1

        sandbox.release.set()
        first_result, duplicate_result = await asyncio.gather(first, duplicate)

        assert first_result == duplicate_result
        assert len(sandbox.requests) == 1
        assert len(runtime.store.programs(session)) == 1
    finally:
        await runtime.model_client.close()


@pytest.mark.asyncio
async def test_completed_exec_id_survives_an_application_restart(tmp_path) -> None:
    settings = Settings(data_dir=tmp_path / "data", broker_socket=tmp_path / "broker.sock")
    first_runtime = ApplicationRuntime(settings)
    first_sandbox = RecordingSandbox()
    first_runtime.sandbox = first_sandbox
    session = first_runtime.store.create_session(SessionCreate(backends=["local"]))
    request = ExecCreate(exec_id="rollout-3:turn-2", code="print('durable')\n")
    try:
        original = await first_runtime.execute_code(session.id, request)
    finally:
        await first_runtime.model_client.close()

    restarted = ApplicationRuntime(settings)
    restarted_sandbox = RecordingSandbox()
    restarted.sandbox = restarted_sandbox
    try:
        replay = await restarted.execute_code(session.id, request)
        assert replay == original
        assert restarted_sandbox.requests == []
    finally:
        await restarted.model_client.close()


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_cancel_idempotent_execution(tmp_path) -> None:
    runtime = ApplicationRuntime(
        Settings(data_dir=tmp_path / "data", broker_socket=tmp_path / "broker.sock")
    )
    sandbox = BlockingSandbox()
    runtime.sandbox = sandbox
    session = runtime.store.create_session(SessionCreate(backends=["local"]))
    request = ExecCreate(exec_id="rollout-4:turn-1", code="print('detached')\n")
    waiter = asyncio.create_task(runtime.execute_code(session.id, request))
    try:
        await sandbox.started.wait()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter

        retry = asyncio.create_task(runtime.execute_code(session.id, request))
        await asyncio.sleep(0)
        assert len(sandbox.requests) == 1
        sandbox.release.set()

        assert (await retry).succeeded is True
        stored = runtime.store.get_exec_record(session, request.exec_id)
        assert stored is not None
        assert stored.status is ExecRecordStatus.COMPLETED
        assert len(sandbox.requests) == 1
    finally:
        sandbox.release.set()
        await asyncio.gather(*tuple(runtime.exec_tasks), return_exceptions=True)
        await runtime.model_client.close()


@pytest.mark.asyncio
async def test_restart_refuses_to_reexecute_a_pending_exec_record(tmp_path) -> None:
    runtime = ApplicationRuntime(
        Settings(data_dir=tmp_path / "data", broker_socket=tmp_path / "broker.sock")
    )
    sandbox = RecordingSandbox()
    runtime.sandbox = sandbox
    session = runtime.store.create_session(SessionCreate(backends=["local"]))
    request = ExecCreate(exec_id="rollout-5:turn-8", code="print('unknown')\n")
    runtime.store.save_exec_record(
        session,
        ExecRecord(
            exec_id=request.exec_id,
            request_hash=runtime._exec_request_hash(request),
            status=ExecRecordStatus.PENDING,
            completed_at=None,
        ),
    )
    try:
        with pytest.raises(ExecIndeterminateError, match="indeterminate prior attempt"):
            await runtime.execute_code(session.id, request)
        assert sandbox.requests == []
    finally:
        await runtime.model_client.close()


def test_workspace_can_be_read_back_before_the_session_is_deleted(tmp_path) -> None:
    """The harness's last chance to keep what the program wrote.

    `artifacts` on an exec result gives names; the contents live only in the
    sandbox and are never rendered to a control model. Once the session is
    deleted they are gone, so a run without this stays re-runnable but stops
    being re-questionable.
    """
    sandbox = RecordingSandbox()
    with exec_client(tmp_path, sandbox) as client:
        session_id = client.post("/v1/sessions", json={"backends": ["local"]}).json()["id"]
        client.post(
            f"/v1/sessions/{session_id}/exec",
            json={"code": "from opensac_sdk import sdk\n"},
        )

        snapshot = client.get(f"/v1/sessions/{session_id}/workspace").json()
        assert snapshot["files"] == [
            {"path": "evidence.jsonl", "bytes": 3, "text": "{}\n", "truncated": False}
        ]
        assert snapshot["omitted"] == []

        client.delete(f"/v1/sessions/{session_id}")
        assert client.get(f"/v1/sessions/{session_id}/workspace").status_code == 404


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


@pytest.mark.asyncio
async def test_delete_marks_closing_waits_for_inflight_exec_and_rejects_new_work(tmp_path) -> None:
    runtime = ApplicationRuntime(
        Settings(data_dir=tmp_path / "data", broker_socket=tmp_path / "broker.sock")
    )
    sandbox = BlockingSandbox()
    runtime.sandbox = sandbox
    session = runtime.store.create_session(SessionCreate(backends=["local"]))
    try:
        running = asyncio.create_task(
            runtime.execute_code(session.id, ExecCreate(code="print('running')\n"))
        )
        await sandbox.started.wait()

        deleting = asyncio.create_task(runtime.close_session(session.id))
        await asyncio.sleep(0)
        assert runtime.store.get_session(session.id).closing is True
        assert deleting.done() is False
        with pytest.raises(SessionClosingError):
            await runtime.execute_code(session.id, ExecCreate(code="print('too late')\n"))

        sandbox.release.set()
        assert (await running).succeeded is True
        assert await deleting is True
        assert sandbox.close_calls == [session.id]
        assert session.token not in runtime.broker.sessions
        with pytest.raises(KeyError):
            runtime.store.get_session(session.id)
    finally:
        await runtime.model_client.close()


@pytest.mark.asyncio
async def test_abort_cancels_inflight_exec_and_is_idempotent(tmp_path) -> None:
    runtime = ApplicationRuntime(
        Settings(data_dir=tmp_path / "data", broker_socket=tmp_path / "broker.sock")
    )
    sandbox = BlockingSandbox()
    runtime.sandbox = sandbox
    session = runtime.store.create_session(SessionCreate(backends=["local"]))
    running = asyncio.create_task(
        runtime.execute_code(
            session.id,
            ExecCreate(exec_id="abort-me", code="print('running')\n"),
        )
    )
    try:
        await sandbox.started.wait()
        assert await runtime.abort_session(session.id) is True
        assert running.cancelled()
        assert sandbox.close_calls == [session.id]
        assert await runtime.abort_session(session.id) is False
        with pytest.raises(KeyError):
            runtime.store.get_session(session.id)
    finally:
        sandbox.release.set()
        await asyncio.gather(running, return_exceptions=True)
        await runtime.model_client.close()


@pytest.mark.asyncio
async def test_concurrent_delete_and_abort_share_one_lifecycle_transition(tmp_path) -> None:
    runtime = ApplicationRuntime(
        Settings(data_dir=tmp_path / "data", broker_socket=tmp_path / "broker.sock")
    )
    sandbox = BlockingCloseSandbox()
    runtime.sandbox = sandbox
    session = runtime.store.create_session(SessionCreate(backends=["local"]))
    deleting = asyncio.create_task(runtime.close_session(session.id))
    try:
        await sandbox.started.wait()
        aborting = asyncio.create_task(runtime.abort_session(session.id))
        await asyncio.sleep(0)
        assert aborting.done() is False

        sandbox.release.set()
        assert await deleting is True
        assert await aborting is False
        assert sandbox.close_calls == [session.id]
        with pytest.raises(KeyError):
            runtime.store.get_session(session.id)
    finally:
        sandbox.release.set()
        await asyncio.gather(deleting, return_exceptions=True)
        await runtime.model_client.close()


@pytest.mark.asyncio
async def test_abort_preempts_graceful_delete_waiting_for_exec(tmp_path) -> None:
    runtime = ApplicationRuntime(
        Settings(data_dir=tmp_path / "data", broker_socket=tmp_path / "broker.sock")
    )
    sandbox = BlockingSandbox()
    runtime.sandbox = sandbox
    session = runtime.store.create_session(SessionCreate(backends=["local"]))
    running = asyncio.create_task(
        runtime.execute_code(session.id, ExecCreate(code="print('running')\n"))
    )
    await sandbox.started.wait()
    deleting = asyncio.create_task(runtime.close_session(session.id))
    try:
        await asyncio.sleep(0)
        assert deleting.done() is False

        aborted, deleted = await asyncio.gather(
            runtime.abort_session(session.id), deleting
        )
        assert running.cancelled()
        assert sorted((aborted, deleted)) == [False, True]
        assert sandbox.close_calls == [session.id]
    finally:
        sandbox.release.set()
        await asyncio.gather(running, deleting, return_exceptions=True)
        await runtime.model_client.close()


@pytest.mark.asyncio
async def test_cleanup_failure_revokes_token_but_keeps_durable_closing_session(
    tmp_path,
) -> None:
    settings = Settings(data_dir=tmp_path / "data", broker_socket=tmp_path / "broker.sock")
    runtime = ApplicationRuntime(settings)
    sandbox = RetryingCloseSandbox()
    runtime.sandbox = sandbox
    session = runtime.store.create_session(SessionCreate(backends=["local"]))
    runtime.bind_session(session)
    try:
        with pytest.raises(SessionCleanupError, match="Sandbox cleanup failed"):
            await runtime.close_session(session.id)

        persisted = runtime.store.get_session(session.id)
        assert persisted.closing is True
        assert session.token not in runtime.broker.sessions

        sandbox.fail = False
        assert await runtime.close_session(session.id) is True
        with pytest.raises(KeyError):
            runtime.store.get_session(session.id)
    finally:
        await runtime.model_client.close()


def test_delete_cleanup_failure_returns_503_and_preserves_session(tmp_path) -> None:
    sandbox = RetryingCloseSandbox()
    with exec_client(tmp_path, sandbox) as client:
        runtime = client.app.state.runtime
        session_id = client.post("/v1/sessions", json={"backends": ["local"]}).json()["id"]
        session = runtime.store.get_session(session_id)
        runtime.bind_session(session)

        response = client.delete(f"/v1/sessions/{session_id}")

        assert response.status_code == 503
        assert runtime.store.get_session(session_id).closing is True
        assert session.token not in runtime.broker.sessions


@pytest.mark.asyncio
async def test_startup_cleanup_failure_keeps_closing_session_for_next_start(
    tmp_path,
) -> None:
    settings = Settings(data_dir=tmp_path / "data", broker_socket=tmp_path / "broker.sock")
    seed = ApplicationRuntime(settings)
    session = seed.store.create_session(SessionCreate(backends=["local"]))
    seed.store.mark_session_closing(session.id)
    await seed.model_client.close()

    failed = ApplicationRuntime(settings)
    failed.sandbox = RetryingCloseSandbox()
    try:
        with pytest.raises(SessionCleanupError):
            await failed.start()
        assert failed.store.get_session(session.id).closing is True
    finally:
        await failed.stop()

    recovered = ApplicationRuntime(settings)
    recovered.sandbox = RetryingCloseSandbox(fail=False)
    try:
        await recovered.start()
        with pytest.raises(KeyError):
            recovered.store.get_session(session.id)
    finally:
        await recovered.stop()


@pytest.mark.asyncio
async def test_immediate_delete_waits_for_an_admitted_exec_before_its_task_starts(
    tmp_path,
) -> None:
    runtime = ApplicationRuntime(
        Settings(data_dir=tmp_path / "data", broker_socket=tmp_path / "broker.sock")
    )
    sandbox = BlockingSandbox()
    runtime.sandbox = sandbox
    session = runtime.store.create_session(SessionCreate(backends=["local"]))
    running = asyncio.create_task(
        runtime.execute_code(
            session.id,
            ExecCreate(exec_id="immediate-delete", code="print('admitted')\n"),
        )
    )
    deleting = asyncio.create_task(runtime.close_session(session.id))
    try:
        await asyncio.wait_for(sandbox.started.wait(), timeout=1)
        assert deleting.done() is False
        assert runtime.store.get_session(session.id).closing is True

        sandbox.release.set()
        assert (await running).succeeded is True
        assert await deleting is True
        assert len(sandbox.requests) == 1
    finally:
        sandbox.release.set()
        await asyncio.gather(running, deleting, return_exceptions=True)
        await runtime.model_client.close()


@pytest.mark.asyncio
async def test_ttl_reaper_removes_only_idle_sessions_and_cleans_broker_state(tmp_path) -> None:
    now = datetime(2026, 8, 10, 12, tzinfo=UTC)
    runtime = ApplicationRuntime(
        Settings(
            data_dir=tmp_path / "data",
            broker_socket=tmp_path / "broker.sock",
            session_ttl_seconds=60,
            session_reaper_interval_seconds=5,
        )
    )
    sandbox = BlockingSandbox()
    runtime.sandbox = sandbox
    stale = runtime.store.create_session(SessionCreate(backends=["local"]))
    fresh = runtime.store.create_session(SessionCreate(backends=["local"]))
    runtime.store.touch_session(stale.id, at=now - timedelta(seconds=61))
    runtime.store.touch_session(fresh.id, at=now - timedelta(seconds=59))
    runtime.bind_session(stale)
    runtime.bind_session(fresh)
    try:
        removed = await runtime.reap_expired_sessions(now=now)

        assert removed == [stale.id]
        assert sandbox.close_calls == [stale.id]
        assert stale.token not in runtime.broker.sessions
        assert fresh.token in runtime.broker.sessions
        assert runtime.store.get_session(fresh.id).id == fresh.id
        with pytest.raises(KeyError):
            runtime.store.get_session(stale.id)
    finally:
        await runtime.model_client.close()


@pytest.mark.asyncio
async def test_per_session_lease_expires_without_global_ttl(tmp_path) -> None:
    now = datetime(2026, 8, 10, 12, tzinfo=UTC)
    runtime = ApplicationRuntime(
        Settings(data_dir=tmp_path / "data", broker_socket=tmp_path / "broker.sock")
    )
    session, _ = await runtime.create_session(
        SessionCreate(backends=["local"], lease_seconds=30)
    )
    runtime.store.touch_session(session.id, at=now - timedelta(seconds=31))
    try:
        assert await runtime.reap_expired_sessions(now=now) == [session.id]
        with pytest.raises(SessionExpiredError):
            runtime.get_session(session.id)
    finally:
        await runtime.model_client.close()


@pytest.mark.asyncio
async def test_ttl_reaper_backs_off_for_an_admitted_exec_before_its_task_starts(
    tmp_path,
) -> None:
    now = datetime(2026, 8, 10, 12, tzinfo=UTC)
    runtime = ApplicationRuntime(
        Settings(
            data_dir=tmp_path / "data",
            broker_socket=tmp_path / "broker.sock",
            session_ttl_seconds=60,
        )
    )
    sandbox = BlockingSandbox()
    runtime.sandbox = sandbox
    session = runtime.store.create_session(SessionCreate(backends=["local"]))
    stale_at = now - timedelta(seconds=61)
    runtime.store.touch_session(session.id, at=stale_at)
    running = asyncio.create_task(
        runtime.execute_code(session.id, ExecCreate(code="print('refresh')\n"))
    )
    reaping = asyncio.create_task(runtime.reap_expired_sessions(now=now))
    try:
        assert await reaping == []
        await asyncio.wait_for(sandbox.started.wait(), timeout=1)
        sandbox.release.set()
        assert (await running).succeeded is True
        assert runtime.store.get_session(session.id).last_access > stale_at
    finally:
        sandbox.release.set()
        await asyncio.gather(running, reaping, return_exceptions=True)
        await runtime.model_client.close()


@pytest.mark.asyncio
async def test_stop_closes_model_client_when_broker_shutdown_fails(tmp_path) -> None:
    runtime = ApplicationRuntime(
        Settings(data_dir=tmp_path / "data", broker_socket=tmp_path / "broker.sock")
    )
    original_model_client = runtime.model_client

    class FailingBrokerRuntime:
        async def stop(self) -> None:
            await runtime.broker.aclose()
            raise RuntimeError("broker stop failed")

    class RecordingModelClient:
        def __init__(self) -> None:
            self.closed = False

        async def close(self) -> None:
            self.closed = True

    model_client = RecordingModelClient()
    runtime.broker_runtime = FailingBrokerRuntime()
    runtime.model_client = model_client
    try:
        with pytest.raises(RuntimeError, match="broker stop failed"):
            await runtime.stop()
        assert model_client.closed is True
    finally:
        await original_model_client.close()


@pytest.mark.asyncio
async def test_lifespan_cleans_up_after_partial_start_failure(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_app(
        Settings(data_dir=tmp_path / "data", broker_socket=tmp_path / "broker.sock")
    )
    runtime = app.state.runtime
    stopped = False

    async def failing_start() -> None:
        raise RuntimeError("partial startup failure")

    async def record_stop() -> None:
        nonlocal stopped
        stopped = True

    monkeypatch.setattr(runtime, "start", failing_start)
    monkeypatch.setattr(runtime, "stop", record_stop)

    with pytest.raises(RuntimeError, match="partial startup failure"):
        async with app.router.lifespan_context(app):
            pass
    assert stopped is True


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
        assert "search.query_many" in payload["capabilities"]

        # The manifest used to over-advertise: it filtered on mechanisms only,
        # so a local-only session still announced `search.web`. One neutral
        # search name makes the manifest correct on both backends by
        # construction rather than by a second filter that could drift.
        web = client.post("/v1/sessions", json={"backends": ["web"]}).json()
        for manifest in (payload["capabilities"], web["capabilities"]):
            assert "search.query" in manifest
            assert not any("web" in method or "local" in method for method in manifest)
            assert not any(method.startswith("llm.") for method in manifest)

        # Recorded on the session, which is what makes an arm recoverable after
        # the run.
        stored = client.app.state.runtime.store.get_session(payload["id"])
        assert stored.mechanisms.llm_subroutine is False


def test_session_advertises_llm_capabilities_only_when_model_is_configured(tmp_path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        broker_socket=tmp_path / "broker.sock",
        model_name="pipeline-model",
    )
    with TestClient(create_app(settings)) as client:
        payload = client.post("/v1/sessions", json={"backends": ["local"]}).json()

    assert "llm.extract_many" in payload["capabilities"]


def test_omitted_mechanisms_default_to_the_unablated_session(tmp_path) -> None:
    with exec_client(tmp_path, RecordingSandbox()) as client:
        payload = client.post("/v1/sessions", json={"backends": ["local"]}).json()
        assert payload["mechanisms"] == {
            "batching": True,
            "persistence": True,
            "llm_subroutine": True,
            "context_decoupling": True,
        }
