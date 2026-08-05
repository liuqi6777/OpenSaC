from __future__ import annotations

import pytest
from opensac_sdk.models import ContentSnippet, SearchHit

from opensac.broker.policy import QuotaExceeded
from opensac.broker.service import BrokerService
from opensac.models import RunLimits, Session


class FakeBackend:
    def __init__(self, name: str) -> None:
        self.name = name

    async def search(self, query, *, limit, domains=None):
        return [
            SearchHit(
                ref="",
                backend=self.name,
                title=query,
                url="https://example.com" if self.name == "web" else None,
                docid="1" if self.name == "local" else None,
                snippet="snippet",
                rank=1,
            )
        ]

    async def content(self, hits, *, query=None):
        return [ContentSnippet(ref=hit.ref, text=f"content:{query}", url=hit.url) for hit in hits]


class BrokenBackend:
    name = "web"

    async def search(self, query, *, limit, domains=None):
        raise RuntimeError("backend exploded")

    async def content(self, hits, *, query=None):
        raise RuntimeError("backend exploded")


def make_session(*, backends=None, max_search_calls=2):
    return Session(
        id="sess_test",
        token="token",
        backends=backends or ["web"],
        limits=RunLimits(max_search_calls=max_search_calls),
        workspace="/tmp/session",
    )


async def test_broker_scopes_references_and_fetches_content() -> None:
    service = BrokerService({"web": FakeBackend("web")})
    service.register_session(make_session())
    hits = await service.call("token", "search.web", {"query": "query", "limit": 3})
    assert hits[0]["ref"].startswith("ref_")
    content = await service.call(
        "token",
        "content.snippets",
        {"refs": [hits[0]["ref"]], "query": "fact"},
    )
    assert content[0]["text"] == "content:fact"
    citations = await service.call("token", "citations.resolve", {"refs": [hits[0]["ref"]]})
    assert citations[0]["url"] == "https://example.com"


async def test_broker_enforces_backend_permissions() -> None:
    service = BrokerService({"local": FakeBackend("local")})
    service.register_session(make_session(backends=["web"]))
    with pytest.raises(PermissionError):
        await service.call("token", "search.local", {"query": "query"})


async def test_broker_enforces_search_quota() -> None:
    service = BrokerService({"web": FakeBackend("web")})
    service.register_session(make_session(max_search_calls=1))
    await service.call("token", "search.web", {"query": "first"})
    with pytest.raises(QuotaExceeded):
        await service.call("token", "search.web", {"query": "second"})


async def test_search_many_raises_when_every_query_fails() -> None:
    service = BrokerService({"web": BrokenBackend()})
    service.register_session(make_session(max_search_calls=5))
    with pytest.raises(RuntimeError, match="backend exploded"):
        await service.call("token", "search.web_many", {"queries": ["one", "two"]})


async def test_search_many_tolerates_partial_failure() -> None:
    service = BrokerService({"web": FakeBackend("web")})
    service.register_session(make_session(max_search_calls=5))
    # An empty query is rejected by the broker while the other one succeeds.
    batches = await service.call("token", "search.web_many", {"queries": ["ok", ""]})
    assert len(batches[0]["hits"]) == 1
    assert batches[0]["error"] is None
    assert batches[1]["hits"] == []
    assert "must not be empty" in batches[1]["error"]
