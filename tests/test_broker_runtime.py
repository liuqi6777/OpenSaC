from __future__ import annotations

import asyncio

from opensac_sdk.models import ContentSnippet, SearchHit
from opensac_sdk.transport import UnixSocketTransport

from opensac.broker import BrokerRuntime, BrokerService
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
        transport = UnixSocketTransport(str(tmp_path / "broker.sock"), "secret")
        result = await asyncio.to_thread(
            transport.call,
            "search.local",
            {"query": "needle", "limit": 1},
        )
        assert result[0]["snippet"] == "needle"
        assert result[0]["ref"].startswith("ref_")
    finally:
        await runtime.stop()
