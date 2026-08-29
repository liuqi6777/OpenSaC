from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import httpx
import pytest
from opensac_sdk._resources import LLMResource, SearchResource
from opensac_sdk.transport import BrokerError, UnixSocketTransport

from opensac.backends.document import DocumentContent, DocumentHandle
from opensac.backends.llm import OpenAICompatibleBackend
from opensac.backends.search import SearchHit
from opensac.broker import BrokerAlreadyRunning, BrokerRuntime, BrokerService, RetrievalRoute
from opensac.models import Mechanisms, ResourceBudget, Session
from opensac.provider import ProviderRequestError


def _broker_service(search_backends, *, document_backends=None, **kwargs):
    if document_backends is None:
        document_backends = search_backends
    routes = {
        name: RetrievalRoute(search=backend, document=document_backends[name])
        for name, backend in search_backends.items()
    }
    return BrokerService(routes, **kwargs)


class SocketBackend:
    name = "local"
    source_kind = "opaque"
    supports_domains = False
    max_depth = None

    async def search(self, query, *, limit, offset=0, domains=None):
        return [
            SearchHit(
                source="",
                backend="local",
                docid="doc-1",
                snippet=query,
                rank=1,
            )
        ]

    async def fetch(self, hit, *, query=None):
        return DocumentContent(source=hit.source, text="full document")

    @staticmethod
    def fetch_candidates(hit: DocumentHandle) -> list[DocumentHandle]:
        return [hit]


async def test_sdk_round_trip_over_real_unix_socket(tmp_path) -> None:
    service = _broker_service({"local": SocketBackend()})
    service.register_session(
        Session(
            id="session",
            token="secret",
            backends=["local"],
            workspace=str(tmp_path / "workspace"),
        )
    )
    runtime = BrokerRuntime(service, tmp_path / "broker.sock")
    await runtime.start()
    try:
        transport = UnixSocketTransport(str(runtime.socket_path), "secret")
        result = await asyncio.to_thread(
            transport.call,
            "search.query",
            {"query": "needle", "limit": 1},
        )
        assert result[0]["snippet"] == "needle"
        assert result[0]["source"] == "doc-1"
        assert "docid" not in result[0]
    finally:
        await runtime.stop()


async def test_search_many_round_trips_concurrently_over_unix_socket(
    tmp_path,
    monkeypatch,
) -> None:
    class ConcurrentBackend(SocketBackend):
        def __init__(self) -> None:
            self.active = 0
            self.max_active = 0
            self.calls: list[str] = []

        async def search(self, query, *, limit, offset=0, domains=None):
            del limit, offset, domains
            self.calls.append(query)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            try:
                await asyncio.sleep({"slow": 0.04, "failed": 0.01, "fast": 0.005}[query])
                if query == "failed":
                    raise ProviderRequestError(
                        "provider_timeout",
                        "Search provider timed out.",
                        retryable=True,
                    )
                return [
                    SearchHit(
                        source="",
                        backend="local",
                        docid=f"doc-{query}",
                        snippet=query,
                        rank=1,
                    )
                ]
            finally:
                self.active -= 1

    backend = ConcurrentBackend()
    service = _broker_service({"local": backend})
    service.register_session(
        Session(
            id="session",
            token="secret",
            backends=["local"],
            workspace=str(tmp_path / "workspace"),
        )
    )
    runtime = BrokerRuntime(service, tmp_path / "broker.sock")
    await runtime.start()
    transport = UnixSocketTransport(str(runtime.socket_path), "secret")
    monkeypatch.setenv("OPENSAC_EXECUTION_ID", "client-many-uds")
    search = SearchResource(transport)
    try:
        outcomes = await asyncio.to_thread(
            search.many,
            ["slow", "failed", "fast"],
            concurrency=2,
        )
        assert [outcome.query for outcome in outcomes] == ["slow", "failed", "fast"]
        assert [outcome.status for outcome in outcomes] == ["success", "failure", "success"]
        assert outcomes[1].error.code == "provider_timeout"
        assert outcomes[1].error.attempts == 1
        assert backend.max_active == 2
        assert sorted(backend.calls) == ["failed", "fast", "slow"]
        fused = search.fuse_rrf(outcomes)
        assert [candidate.source for candidate in fused] == ["doc-slow", "doc-fast"]
        assert [candidate.provenance[0].input_index for candidate in fused] == [0, 2]
        trace = service.take_trace("secret", "client-many-uds")
        assert [event.method for event in trace].count("session.capabilities") == 1
        assert [event.method for event in trace].count("search.query") == 3
    finally:
        transport.close()
        await runtime.stop()


async def test_search_many_allows_partial_budget_success_over_unix_socket(
    tmp_path,
) -> None:
    class CountingBackend(SocketBackend):
        def __init__(self) -> None:
            self.calls = 0

        async def search(self, query, *, limit, offset=0, domains=None):
            self.calls += 1
            await asyncio.sleep(0)
            return await super().search(
                query,
                limit=limit,
                offset=offset,
                domains=domains,
            )

    backend = CountingBackend()
    service = _broker_service({"local": backend})
    state = service.register_session(
        Session(
            id="session",
            token="secret",
            backends=["local"],
            workspace=str(tmp_path / "workspace"),
            budget=ResourceBudget(max_search_queries=2),
        )
    )
    runtime = BrokerRuntime(service, tmp_path / "broker.sock")
    await runtime.start()
    transport = UnixSocketTransport(str(runtime.socket_path), "secret")
    try:
        outcomes = await asyncio.to_thread(
            SearchResource(transport).many,
            ["one", "two", "three", "four"],
            concurrency=4,
        )
        assert sum(outcome.status == "success" for outcome in outcomes) == 2
        assert (
            sum(outcome.error.code == "budget_exhausted" for outcome in outcomes if outcome.error)
            == 2
        )
        assert backend.calls == 2
        assert state.policy.usage.search_calls == 2
    finally:
        transport.close()
        await runtime.stop()


async def test_search_many_honours_batching_mechanism_over_unix_socket(
    tmp_path,
) -> None:
    service = _broker_service({"local": SocketBackend()})
    state = service.register_session(
        Session(
            id="session",
            token="secret",
            backends=["local"],
            workspace=str(tmp_path / "workspace"),
            mechanisms=Mechanisms(batching=False),
        )
    )
    runtime = BrokerRuntime(service, tmp_path / "broker.sock")
    await runtime.start()
    transport = UnixSocketTransport(str(runtime.socket_path), "secret")
    try:
        with pytest.raises(BrokerError) as raised:
            await asyncio.to_thread(
                SearchResource(transport).many,
                ["one", "two"],
            )
        assert raised.value.code == "capability_disabled"
        assert state.policy.usage.search_calls == 0
    finally:
        transport.close()
        await runtime.stop()


async def test_search_many_registers_shared_source_by_completion_order(
    tmp_path,
) -> None:
    class CompletionOrderBackend(SocketBackend):
        async def search(self, query, *, limit, offset=0, domains=None):
            del limit, offset, domains
            if query == "first":
                await asyncio.sleep(0.03)
            return [
                SearchHit(
                    source="",
                    backend="local",
                    title=query,
                    docid="shared",
                    snippet=query,
                    rank=1,
                )
            ]

    service = _broker_service({"local": CompletionOrderBackend()})
    state = service.register_session(
        Session(
            id="session",
            token="secret",
            backends=["local"],
            workspace=str(tmp_path / "workspace"),
        )
    )
    runtime = BrokerRuntime(service, tmp_path / "broker.sock")
    await runtime.start()
    transport = UnixSocketTransport(str(runtime.socket_path), "secret")
    try:
        outcomes = await asyncio.to_thread(
            SearchResource(transport).many,
            ["first", "second"],
            concurrency=2,
        )
        assert [outcome.query for outcome in outcomes] == ["first", "second"]
        source = outcomes[0].hits[0].source
        assert outcomes[1].hits[0].source == source
        remembered = state.document_for_alias(source)
        assert remembered is not None and remembered.handle.title == "second"
    finally:
        transport.close()
        await runtime.stop()


async def test_search_many_cancellation_drains_socket_calls_and_threads(
    tmp_path,
    monkeypatch,
) -> None:
    class BlockingBackend(SocketBackend):
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.started_count = 0
            self.cancelled_count = 0

        async def search(self, query, *, limit, offset=0, domains=None):
            del query, limit, offset, domains
            self.started_count += 1
            if self.started_count == 2:
                self.started.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled_count += 1
                raise
            return []

    backend = BlockingBackend()
    service = _broker_service({"local": backend})
    service.register_session(
        Session(
            id="session",
            token="secret",
            backends=["local"],
            workspace=str(tmp_path / "workspace"),
        )
    )
    runtime = BrokerRuntime(service, tmp_path / "broker.sock")
    await runtime.start()
    transport = UnixSocketTransport(str(runtime.socket_path), "secret")
    monkeypatch.setenv("OPENSAC_EXECUTION_ID", "client-many-cancel")
    running = asyncio.create_task(
        asyncio.to_thread(
            SearchResource(transport).many,
            ["one", "two"],
            concurrency=2,
        )
    )
    try:
        await asyncio.wait_for(backend.started.wait(), timeout=2)
        cancelled = await service.cancel_execution(
            "secret", "client-many-cancel", reason="test_cancelled"
        )
        assert cancelled >= 2
        assert ("secret", "client-many-cancel") not in service.execution_tasks

        with pytest.raises(BrokerError) as raised:
            await asyncio.wait_for(running, timeout=2)
        assert raised.value.code == "broker_transport_error"
        assert backend.cancelled_count == 2
        assert not any(thread.name.startswith("opensac-sdk") for thread in threading.enumerate())

        trace = service.take_trace("secret", "client-many-cancel")
        search_events = [event for event in trace if event.method == "search.query"]
        assert len(search_events) == 2
        assert all(event.status == "cancelled" for event in search_events)
    finally:
        backend.release.set()
        if not running.done():
            await service.cancel_execution("secret", "client-many-cancel")
            await asyncio.gather(running, return_exceptions=True)
        transport.close()
        await runtime.stop()


async def test_broker_rejects_missing_or_mismatched_capability_contract(tmp_path) -> None:
    service = _broker_service({"local": SocketBackend()})
    service.register_session(
        Session(
            id="session",
            token="secret",
            backends=["local"],
            workspace=str(tmp_path / "workspace"),
        )
    )
    runtime = BrokerRuntime(service, tmp_path / "broker.sock")
    await runtime.start()

    def raw_call(reported_contract: str | None) -> httpx.Response:
        headers = {"Authorization": "Bearer secret"}
        if reported_contract is not None:
            headers["X-OpenSAC-Capability-Contract"] = reported_contract
        transport = httpx.HTTPTransport(uds=str(runtime.socket_path))
        with httpx.Client(transport=transport, base_url="http://opensac") as client:
            return client.post(
                "/v1/call",
                json={"method": "session.capabilities", "params": {}},
                headers=headers,
            )

    try:
        for reported_contract in (None, "14"):
            response = await asyncio.to_thread(raw_call, reported_contract)
            assert response.status_code == 200
            payload = response.json()
            assert payload["capability_contract"] == 15
            assert payload["ok"] is False
            assert payload["error"]["code"] == "capability_contract_mismatch"
            assert "broker requires 15" in payload["error"]["message"]
    finally:
        await runtime.stop()


async def test_broker_round_trip_returns_contract_v2_errors(tmp_path) -> None:
    service = _broker_service({"local": SocketBackend()})
    service.register_session(
        Session(
            id="session",
            token="secret",
            backends=["local"],
            workspace=str(tmp_path / "workspace"),
        )
    )
    runtime = BrokerRuntime(service, tmp_path / "broker.sock")
    await runtime.start()
    try:
        transport = UnixSocketTransport(str(runtime.socket_path), "secret")
        with pytest.raises(BrokerError, match="query must not be empty") as raised:
            await asyncio.to_thread(
                transport.call,
                "search.query",
                {"query": "", "limit": 1},
            )
        assert raised.value.code == "invalid_request"
        assert raised.value.retryable is False
        assert raised.value.attempts == 0

        with pytest.raises(BrokerError, match="include_domain") as raised:
            await asyncio.to_thread(
                transport.call,
                "search.query",
                {"query": "q", "include_domain": ["example.com"]},
            )
        assert raised.value.code == "invalid_request"
        assert raised.value.retryable is False

        with pytest.raises(BrokerError, match="Unsupported capability") as raised:
            await asyncio.to_thread(transport.call, "session.usage", {})
        assert raised.value.code == "invalid_request"
        assert raised.value.retryable is False

        denied = UnixSocketTransport(str(runtime.socket_path), "unknown-token")
        with pytest.raises(BrokerError, match="Unknown or expired") as raised:
            await asyncio.to_thread(denied.call, "session.capabilities", {})
        assert raised.value.code == "permission_denied"
        assert raised.value.retryable is False
        assert raised.value.attempts == 0
    finally:
        await runtime.stop()


async def test_provider_error_details_round_trip_over_real_unix_socket(tmp_path) -> None:
    class LimitedBackend(SocketBackend):
        async def search(self, query, *, limit, offset=0, domains=None):
            raise ProviderRequestError(
                "provider_rate_limited",
                "Provider rate limit was exceeded.",
                retryable=True,
                provider_status=429,
                retry_after_seconds=2.5,
            )

    service = _broker_service({"local": LimitedBackend()})
    service.register_session(
        Session(
            id="session",
            token="secret",
            backends=["local"],
            workspace=str(tmp_path / "workspace"),
        )
    )
    runtime = BrokerRuntime(service, tmp_path / "broker.sock")
    await runtime.start()
    try:
        transport = UnixSocketTransport(str(runtime.socket_path), "secret")
        with pytest.raises(BrokerError) as raised:
            await asyncio.to_thread(
                transport.call,
                "search.query",
                {"query": "limited", "limit": 1},
            )
        assert raised.value.code == "provider_rate_limited"
        assert raised.value.retryable is True
        assert raised.value.attempts == 1
        assert raised.value.provider_status == 429
        assert raised.value.retry_after_seconds == 2.5
        assert raised.value.provider == "local"
        assert raised.value.component == "search"
        assert raised.value.scope == "provider"
    finally:
        await runtime.stop()


class EchoModelClient:
    def __init__(self) -> None:
        class Completions:
            async def create(self, **kwargs):
                content = kwargs["messages"][-1]["content"].upper()
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
                )

        self.chat = SimpleNamespace(completions=Completions())

    async def close(self) -> None:
        return None


class FailingModelClient:
    def __init__(self) -> None:
        class Completions:
            async def create(self, **kwargs):
                raise RuntimeError("provider response that must not cross the broker")

        self.chat = SimpleNamespace(completions=Completions())

    async def close(self) -> None:
        return None


async def test_extraction_provider_failure_is_typed_and_sanitized(tmp_path) -> None:
    service = _broker_service(
        {"local": SocketBackend()},
        llm_backend=OpenAICompatibleBackend(model="test-model", client=FailingModelClient()),
    )
    service.register_session(
        Session(
            id="session",
            token="secret",
            backends=["local"],
            workspace=str(tmp_path / "workspace"),
        )
    )
    runtime = BrokerRuntime(service, tmp_path / "broker.sock")
    await runtime.start()
    try:
        resource = LLMResource(UnixSocketTransport(str(runtime.socket_path), "secret"))
        with pytest.raises(BrokerError) as failed:
            await asyncio.to_thread(
                resource.extract,
                {"value": 1},
                instruction="Copy the value.",
                schema={
                    "type": "object",
                    "properties": {"value": {"type": "integer"}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
            )
        assert failed.value.code == "provider_invalid_response"
        assert failed.value.retryable is False
        assert "provider response" not in str(failed.value)
    finally:
        await runtime.stop()


async def test_llm_resource_round_trips_over_real_unix_socket(tmp_path) -> None:
    """Cover the SDK -> socket -> broker -> model path, not just the handler.

    The unit tests call BrokerService directly, which would not catch an SDK
    method whose params do not match what the handler reads.
    """
    service = _broker_service(
        {"local": SocketBackend()},
        llm_backend=OpenAICompatibleBackend(model="test-model", client=EchoModelClient()),
    )
    service.register_session(
        Session(
            id="session",
            token="secret",
            backends=["local"],
            workspace=str(tmp_path / "workspace"),
        )
    )
    runtime = BrokerRuntime(service, tmp_path / "broker.sock")
    await runtime.start()
    try:
        resource = LLMResource(UnixSocketTransport(str(runtime.socket_path), "secret"))
        assert await asyncio.to_thread(resource.complete, "plan") == "PLAN"
        assert [await asyncio.to_thread(resource.complete, prompt) for prompt in ["a", "b"]] == [
            "A",
            "B",
        ]
        assert service.sessions["secret"].policy.usage.llm_calls == 3
    finally:
        await runtime.stop()


async def test_second_broker_refuses_to_evict_a_live_socket(tmp_path) -> None:
    """Regression: a second instance used to unlink the first one's socket.

    That did not stop the first process. It kept its bound socket and kept
    answering health checks, while every sandbox started afterwards failed to
    bind a mount source that no longer existed -- containers exiting 125 with
    no hint that a stray `serve` was the cause.
    """
    socket_path = tmp_path / "broker.sock"
    service = _broker_service({"local": SocketBackend()})
    service.register_session(
        Session(
            id="session",
            token="secret",
            backends=["local"],
            workspace=str(tmp_path / "workspace"),
        )
    )
    first = BrokerRuntime(service, socket_path)
    await first.start()
    try:
        second = BrokerRuntime(_broker_service({"local": SocketBackend()}), socket_path)
        with pytest.raises(BrokerAlreadyRunning):
            await second.start()

        # The loser must leave the winner's socket alone, on the way in and on
        # the way back out.
        assert first.socket_path.exists()
        await second.stop()
        assert first.socket_path.exists()

        transport = UnixSocketTransport(str(first.socket_path), "secret")
        hits = await asyncio.to_thread(transport.call, "search.query", {"query": "q", "limit": 1})
        assert hits[0]["snippet"] == "q"
    finally:
        await first.stop()
    assert not first.socket_path.exists()


async def test_broker_replaces_a_stale_socket_file(tmp_path) -> None:
    # A file left by a process that died without cleanup has nothing listening
    # on it, so bind() would fail with EADDRINUSE unless it is removed first.
    socket_path = tmp_path / "broker.sock"
    socket_path.write_bytes(b"")
    runtime = BrokerRuntime(_broker_service({"local": SocketBackend()}), socket_path)
    await runtime.start()
    try:
        assert runtime.socket_path.is_socket()
    finally:
        await runtime.stop()
