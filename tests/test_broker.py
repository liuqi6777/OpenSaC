from __future__ import annotations

from types import SimpleNamespace

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


class FakeModelClient:
    """Minimal stand-in for the AsyncOpenAI surface the broker touches."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        parent = self

        class Completions:
            async def create(self, **kwargs):
                parent.calls.append(kwargs)
                prompt = kwargs["messages"][-1]["content"]
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content=f"echo:{prompt}"))]
                )

        self.chat = SimpleNamespace(completions=Completions())


def make_llm_service(**kwargs) -> tuple[BrokerService, FakeModelClient]:
    client = FakeModelClient()
    service = BrokerService(
        {"web": FakeBackend("web")},
        model_client=client,
        extraction_model="test-model",
    )
    service.register_session(
        Session(
            id="sess_test",
            token="token",
            backends=["web"],
            limits=RunLimits(**kwargs),
            workspace="/tmp/session",
        )
    )
    return service, client


async def test_llm_complete_passes_system_prompt_and_charges_one_call() -> None:
    service, client = make_llm_service(max_llm_calls=2)
    answer = await service.call(
        "token",
        "llm.complete",
        {"prompt": "plan the next queries", "system": "be terse", "temperature": 0.7},
    )
    assert answer == "echo:plan the next queries"
    assert client.calls[0]["messages"][0] == {"role": "system", "content": "be terse"}
    assert client.calls[0]["temperature"] == 0.7
    # complete() is free-form, so it must not force JSON mode the way extract does.
    assert "response_format" not in client.calls[0]
    assert service.sessions["token"].policy.usage.llm_calls == 1


async def test_llm_complete_many_preserves_prompt_order() -> None:
    service, _ = make_llm_service(max_llm_calls=5)
    answers = await service.call(
        "token",
        "llm.complete_many",
        {"prompts": ["one", "two", "three"], "concurrency": 3},
    )
    assert answers == ["echo:one", "echo:two", "echo:three"]
    assert service.sessions["token"].policy.usage.llm_calls == 3


async def test_llm_complete_many_charges_the_whole_fanout_before_running() -> None:
    service, client = make_llm_service(max_llm_calls=2)
    with pytest.raises(QuotaExceeded):
        await service.call("token", "llm.complete_many", {"prompts": ["one", "two", "three"]})
    # Nothing ran, so the caller is not left guessing which prompts were charged.
    assert client.calls == []
    assert service.sessions["token"].policy.usage.llm_calls == 0


async def test_llm_calls_fail_when_no_model_is_configured() -> None:
    service = BrokerService({"web": FakeBackend("web")})
    service.register_session(make_session())
    with pytest.raises(RuntimeError, match="not configured"):
        await service.call("token", "llm.complete", {"prompt": "hello"})
