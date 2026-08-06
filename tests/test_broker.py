from __future__ import annotations

import json
from pathlib import Path
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
                    choices=[SimpleNamespace(message=SimpleNamespace(content=f"echo:{prompt}"))],
                    usage=SimpleNamespace(total_tokens=11),
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
    assert service.sessions["token"].policy.usage.pipeline_model_tokens == 11


async def test_capability_trace_records_compact_inputs_results_and_errors() -> None:
    service = BrokerService({"web": FakeBackend("web")})
    service.register_session(make_session(max_search_calls=5))
    await service.call(
        "token",
        "search.web_many",
        {"queries": ["one", "two"], "limit_per_query": 1},
        execution_id="exec-1",
    )
    with pytest.raises(ValueError):
        await service.call(
            "token",
            "content.get_many",
            {"refs": ["missing"]},
            execution_id="exec-1",
        )

    trace = service.take_trace("token", "exec-1")
    assert [event.method for event in trace] == ["search.web_many", "content.get_many"]
    assert trace[0].queries == ["one", "two"]
    assert trace[0].input_count == 2
    assert trace[0].result_count == 2
    assert trace[1].status == "error"
    assert trace[1].error_type == "ValueError"
    assert "missing" not in str(trace[0].model_dump())


async def test_query_aware_snippets_select_the_relevant_paragraph() -> None:
    class PassageBackend(FakeBackend):
        async def content(self, hits, *, query=None):
            del query
            text = (
                "An unrelated introduction about cooking and weather.\n\n"
                "Vector databases use HNSW indexes for approximate nearest-neighbor search.\n\n"
                "An unrelated conclusion about travel."
            )
            return [ContentSnippet(ref=hit.ref, text=text) for hit in hits]

    service = BrokerService({"web": PassageBackend("web")})
    state = service.register_session(make_session())
    hits = await service.call("token", "search.web", {"query": "seed"})
    snippets = await service.call(
        "token",
        "content.snippets",
        {
            "query": "HNSW nearest neighbor",
            "refs": [hits[0]["ref"]],
            "max_tokens_per_page": 12,
        },
    )
    assert "HNSW indexes" in snippets[0]["text"]
    assert "cooking" not in snippets[0]["text"]
    assert snippets[0]["metadata"]["passage_index"] == 1
    assert snippets[0]["metadata"]["passage_score"] > 0
    assert state.policy.usage.content_fetches == 1


def test_query_aware_passage_matches_shared_golden_fixture() -> None:
    fixture = json.loads(
        (Path(__file__).parent / "data" / "query_aware_passage.json").read_text(
            encoding="utf-8"
        )
    )
    text, metadata = BrokerService._select_passage(
        fixture["text"], fixture["goal"], fixture["max_chars"]
    )
    assert text == fixture["expected_text"]
    assert metadata["passage_index"] == fixture["expected_passage_index"]


async def test_llm_complete_many_preserves_prompt_order() -> None:
    service, _ = make_llm_service(max_llm_calls=5)
    answers = await service.call(
        "token",
        "llm.complete_many",
        {"prompts": ["one", "two", "three"], "concurrency": 3},
        execution_id="exec-many",
    )
    assert answers == ["echo:one", "echo:two", "echo:three"]
    assert service.sessions["token"].policy.usage.llm_calls == 3
    assert service.take_trace("token", "exec-many")[0].model_tokens == 33


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
