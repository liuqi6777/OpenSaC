from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from opensac_sdk.llm import LLMResource
from opensac_sdk.models import ContentSnippet, SearchHit
from opensac_sdk.transport import UnixSocketTransport

from opensac.broker import BrokerAlreadyRunning, BrokerRuntime, BrokerService
from opensac.models import RunLimits, Session


class SocketBackend:
    name = "local"

    async def search(self, query, *, limit, domains=None):
        return [
            SearchHit(
                ref="",
                backend="local",
                docid="doc-1",
                snippet=query,
                rank=1,
            )
        ]

    async def content(self, hits, *, query=None):
        return [ContentSnippet(ref=hit.ref, text="full document") for hit in hits]


async def test_sdk_round_trip_over_real_unix_socket(tmp_path) -> None:
    service = BrokerService({"local": SocketBackend()})
    service.register_session(
        Session(
            id="session",
            token="secret",
            backends=["local"],
            limits=RunLimits(),
            workspace=str(tmp_path / "workspace"),
        )
    )
    runtime = BrokerRuntime(service, tmp_path / "broker.sock")
    await runtime.start()
    try:
        transport = UnixSocketTransport(str(runtime.socket_path), "secret")
        result = await asyncio.to_thread(
            transport.call,
            "search.local",
            {"query": "needle", "limit": 1},
        )
        assert result[0]["snippet"] == "needle"
        assert result[0]["ref"].startswith("ref_")
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


async def test_llm_resource_round_trips_over_real_unix_socket(tmp_path) -> None:
    """Cover the SDK -> socket -> broker -> model path, not just the handler.

    The unit tests call BrokerService directly, which would not catch an SDK
    method whose params do not match what the handler reads.
    """
    service = BrokerService(
        {"local": SocketBackend()},
        model_client=EchoModelClient(),
        extraction_model="test-model",
    )
    service.register_session(
        Session(
            id="session",
            token="secret",
            backends=["local"],
            limits=RunLimits(max_llm_calls=10),
            workspace=str(tmp_path / "workspace"),
        )
    )
    runtime = BrokerRuntime(service, tmp_path / "broker.sock")
    await runtime.start()
    try:
        resource = LLMResource(UnixSocketTransport(str(runtime.socket_path), "secret"))
        assert await asyncio.to_thread(resource.complete, "plan") == "PLAN"
        assert await asyncio.to_thread(resource.complete_many, ["a", "b"]) == ["A", "B"]
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
    service = BrokerService({"local": SocketBackend()})
    service.register_session(
        Session(
            id="session",
            token="secret",
            backends=["local"],
            limits=RunLimits(),
            workspace=str(tmp_path / "workspace"),
        )
    )
    first = BrokerRuntime(service, socket_path)
    await first.start()
    try:
        second = BrokerRuntime(BrokerService({"local": SocketBackend()}), socket_path)
        with pytest.raises(BrokerAlreadyRunning):
            await second.start()

        # The loser must leave the winner's socket alone, on the way in and on
        # the way back out.
        assert first.socket_path.exists()
        await second.stop()
        assert first.socket_path.exists()

        transport = UnixSocketTransport(str(first.socket_path), "secret")
        hits = await asyncio.to_thread(transport.call, "search.local", {"query": "q", "limit": 1})
        assert hits[0]["snippet"] == "q"
    finally:
        await first.stop()
    assert not first.socket_path.exists()


async def test_broker_replaces_a_stale_socket_file(tmp_path) -> None:
    # A file left by a process that died without cleanup has nothing listening
    # on it, so bind() would fail with EADDRINUSE unless it is removed first.
    socket_path = tmp_path / "broker.sock"
    socket_path.write_bytes(b"")
    runtime = BrokerRuntime(BrokerService({"local": SocketBackend()}), socket_path)
    await runtime.start()
    try:
        assert runtime.socket_path.is_socket()
    finally:
        await runtime.stop()
