import json

import httpx
import pytest

from opensac.client import AsyncOpenSAC, OpenSAC


class ClientServer:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict | None]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content) if request.content else None
        self.requests.append((request.method, request.url.path, payload))
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"status": "ok"})
        if request.method == "POST" and request.url.path == "/v1/sessions":
            return httpx.Response(
                200,
                json={"id": "sess-new", "features": ["idempotent_exec"]},
            )
        if request.method == "GET" and request.url.path == "/v1/sessions/sess-old":
            return httpx.Response(200, json={"id": "sess-old"})
        if request.method == "POST" and request.url.path.endswith("/exec"):
            return httpx.Response(200, json={"succeeded": True})
        if request.method == "POST" and request.url.path.endswith("/heartbeat"):
            return httpx.Response(
                200,
                json={"id": "sess-new", "features": ["idempotent_exec"]},
            )
        if request.method == "POST" and request.url.path.endswith("/abort"):
            return httpx.Response(200, json={"status": "aborted"})
        if request.method == "POST" and request.url.path == "/v1/admin/drain":
            return httpx.Response(200, json={"status": "draining"})
        if request.method == "DELETE":
            return httpx.Response(200)
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")


def test_sync_client_negotiates_idempotency_and_preserves_explicit_options() -> None:
    server = ClientServer()
    client = OpenSAC()
    client.close()
    client._client = httpx.Client(
        base_url="http://test",
        transport=httpx.MockTransport(server),
    )
    try:
        session = client.create_session(
            backends=[],
            mechanisms={"persistence": False},
        )
        assert session["features"] == ["idempotent_exec"]
        assert server.requests[0][2] == {
            "backends": [],
            "limits": {},
            "mechanisms": {"persistence": False},
        }
        assert client.health() == {"status": "ok"}
        client.exec_code("sess-new", "pass\n", exec_id="logical-1")
        assert server.requests[-1][2]["exec_id"] == "logical-1"
        assert client.heartbeat_session("sess-new")["id"] == "sess-new"
        assert client.abort_session("sess-new") == {"status": "aborted"}
        assert client.drain_worker() == {"status": "draining"}

        with pytest.raises(RuntimeError, match="does not advertise idempotent_exec"):
            client.exec_code("sess-old", "pass\n", exec_id="unsafe-retry")
    finally:
        client.close()


@pytest.mark.asyncio
async def test_async_client_negotiates_idempotency() -> None:
    server = ClientServer()
    client = AsyncOpenSAC()
    await client.close()
    client._client = httpx.AsyncClient(
        base_url="http://test",
        transport=httpx.MockTransport(server),
    )
    try:
        await client.create_session(
            request_id="rollout-1",
            lease_seconds=60,
            budget={"max_exec_calls": 4},
        )
        assert server.requests[0][2]["request_id"] == "rollout-1"
        assert server.requests[0][2]["lease_seconds"] == 60
        assert server.requests[0][2]["budget"] == {"max_exec_calls": 4}
        await client.exec_code("sess-new", "pass\n", exec_id="logical-1")
        assert server.requests[-1][2]["exec_id"] == "logical-1"

        with pytest.raises(RuntimeError, match="does not advertise idempotent_exec"):
            await client.exec_code("sess-old", "pass\n", exec_id="unsafe-retry")
    finally:
        await client.close()
