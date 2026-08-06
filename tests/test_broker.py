from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from opensac_sdk.models import ContentSnippet, SearchHit

from opensac.broker.policy import MechanismDisabled, QuotaExceeded
from opensac.broker.service import BrokerService
from opensac.models import CAPABILITY_METHODS, Mechanisms, RunLimits, Session


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


def make_session(*, backends=None, max_search_calls=2, mechanisms=None):
    return Session(
        id="sess_test",
        token="token",
        backends=backends or ["web"],
        limits=RunLimits(max_search_calls=max_search_calls),
        workspace="/tmp/session",
        mechanisms=mechanisms or Mechanisms(),
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


class RankedBackend:
    """Returns a fixed document set, so the same page recurs across queries."""

    name = "web"

    def __init__(self, urls: list[str]) -> None:
        self.urls = urls

    async def search(self, query, *, limit, domains=None):
        return [
            SearchHit(
                ref="",
                backend="web",
                title=f"{query}-{index}",
                url=url,
                snippet="snippet",
                score=1.0 / (index + 1),
                rank=index + 1,
            )
            for index, url in enumerate(self.urls[:limit])
        ]

    async def content(self, hits, *, query=None):
        return [ContentSnippet(ref=hit.ref, text="body", url=hit.url) for hit in hits]


async def test_the_same_document_keeps_one_ref_across_queries() -> None:
    """Two queries surfacing one page must hand back one handle.

    With a fresh random ref per sighting the program cannot tell that it already
    has the page, so it re-fetches it and double-counts the evidence -- and no
    two runs of the same recorded search produce the same refs, which is what
    makes a trajectory unreplayable.
    """
    service = BrokerService({"web": RankedBackend(["https://example.com/a"])})
    state = service.register_session(make_session(max_search_calls=5))

    first = await service.call("token", "search.web", {"query": "one"})
    second = await service.call("token", "search.web", {"query": "two"})

    assert first[0]["ref"] == second[0]["ref"]
    assert len(state.references) == 1


async def test_refs_are_opaque_and_reproducible() -> None:
    """Same document, different process: same handle."""
    urls = ["https://Example.com/a?utm_source=news&id=7#section"]
    left = BrokerService({"web": RankedBackend(urls)})
    left.register_session(make_session())
    right = BrokerService({"web": RankedBackend(urls)})
    right.register_session(make_session())

    one = (await left.call("token", "search.web", {"query": "q"}))[0]["ref"]
    two = (await right.call("token", "search.web", {"query": "q"}))[0]["ref"]

    assert one == two
    assert one.startswith("ref_")
    # Opaque: nothing a program could have constructed for itself.
    assert "example.com" not in one


def test_canonical_url_folds_only_what_is_safe_to_fold() -> None:
    canonical = BrokerService._canonical_url
    assert canonical("HTTPS://Example.COM/a?utm_source=x&id=7#frag") == (
        "https://example.com/a?id=7"
    )
    # Order of surviving parameters must not decide identity.
    assert canonical("https://e.com/p?b=2&a=1") == canonical("https://e.com/p?a=1&b=2")
    # Paths are left alone: /a and /a/ can be different pages and nothing here
    # can prove otherwise.
    assert canonical("https://e.com/a") != canonical("https://e.com/a/")


async def test_trace_records_identity_and_rank_for_every_hit() -> None:
    """Rank and duplication cannot be recovered after the fact.

    A baseline that logged only `result_count` can never be asked afterwards
    whether ranking or duplicate candidates were the bottleneck -- which is
    exactly the question that decides whether a fusion/dedup layer is worth
    building.
    """
    service = BrokerService(
        {"web": RankedBackend(["https://example.com/a", "https://example.com/b"])}
    )
    service.register_session(make_session(max_search_calls=5))

    await service.call(
        "token",
        "search.web_many",
        {"queries": ["one", "two"], "limit_per_query": 2},
        execution_id="exec-hits",
    )
    event = service.take_trace("token", "exec-hits")[0]

    # A fan-out lands in one event, so per-query duplication is visible in it.
    assert len(event.hits) == 4
    assert [hit.rank for hit in event.hits] == [1, 2, 1, 2]
    assert len({hit.identity for hit in event.hits}) == 2
    assert event.hits[0].score == 1.0
    # Addresses yes, page text no.
    assert "snippet" not in event.model_dump_json()


async def test_batching_disabled_forces_one_item_per_call() -> None:
    """The switch bounds fan-out; it does not remove the method.

    Removing `*_many` outright would also remove structured extraction, since
    `llm.extract_many` has no singular form, and the arm would then be measuring
    two things at once.
    """
    service = BrokerService({"web": RankedBackend(["https://example.com/a"])})
    service.register_session(
        make_session(max_search_calls=5, mechanisms=Mechanisms(batching=False))
    )

    with pytest.raises(MechanismDisabled, match="at most one item"):
        await service.call(
            "token",
            "search.web_many",
            {"queries": ["one", "two"]},
            execution_id="exec-block",
        )
    batches = await service.call("token", "search.web_many", {"queries": ["one"]})
    assert len(batches[0]["hits"]) == 1

    # A blocked call is still an event: an arm that disables a capability wants
    # to know how often the model kept reaching for it.
    blocked = service.take_trace("token", "exec-block")[0]
    assert blocked.status == "error"
    assert blocked.error_type == "MechanismDisabled"


async def test_llm_subroutine_disabled_blocks_the_whole_capability_class() -> None:
    service, client = make_llm_service(max_llm_calls=5)
    service.sessions["token"].session.mechanisms = Mechanisms(llm_subroutine=False)

    with pytest.raises(MechanismDisabled, match="plain Python"):
        await service.call("token", "llm.complete", {"prompt": "plan"})
    assert client.calls == []
    # Blocked before the quota is touched, so the arm does not also change budget.
    assert service.sessions["token"].policy.usage.llm_calls == 0


async def test_context_decoupling_disabled_echoes_results_into_the_trace() -> None:
    """The arm that separates "can orchestrate" from "middle never reaches context".

    Same interface, same expressiveness -- only the results come back, so the
    caller can put them in the control model's conversation.
    """
    service = BrokerService({"web": RankedBackend(["https://example.com/a"])})
    service.register_session(
        make_session(mechanisms=Mechanisms(context_decoupling=False))
    )

    hits = await service.call(
        "token", "search.web", {"query": "q"}, execution_id="exec-echo"
    )
    event = service.take_trace("token", "exec-echo")[0]
    assert event.result_payload == hits
    assert event.result_payload_truncated is False


async def test_default_sessions_keep_results_out_of_the_trace() -> None:
    service = BrokerService({"web": RankedBackend(["https://example.com/a"])})
    service.register_session(make_session())
    await service.call("token", "search.web", {"query": "q"}, execution_id="exec-plain")
    event = service.take_trace("token", "exec-plain")[0]
    assert event.result_payload is None


async def test_oversized_payload_is_capped_and_says_so() -> None:
    service = BrokerService(
        {"web": RankedBackend([f"https://example.com/{index}" for index in range(50)])},
        max_context_payload_bytes=200,
    )
    service.register_session(
        make_session(mechanisms=Mechanisms(context_decoupling=False))
    )
    await service.call(
        "token", "search.web", {"query": "q", "limit": 50}, execution_id="exec-big"
    )
    event = service.take_trace("token", "exec-big")[0]
    assert event.result_payload_truncated is True
    assert len(event.result_payload) == 200


async def test_capability_methods_stay_in_step_with_the_handler_table() -> None:
    """CAPABILITY_METHODS drives the session manifest and so the skill text.

    A capability added on one side only is either invisible to the model or
    advertised to it without an implementation, and both cost a turn to find
    out. The assertion lives on the dispatch path, so any call exercises it.
    """
    service = BrokerService({"web": RankedBackend(["https://example.com/a"])})
    service.register_session(make_session())
    with pytest.raises(ValueError, match="Unsupported capability"):
        await service.call("token", "search.nope", {})


def test_capabilities_manifest_drops_only_what_is_disabled() -> None:
    assert Mechanisms().capabilities() == list(CAPABILITY_METHODS)
    without_llm = Mechanisms(llm_subroutine=False).capabilities()
    assert not any(method.startswith("llm.") for method in without_llm)
    assert "search.web_many" in without_llm
    # Batching bounds a call's width rather than removing it, so the method is
    # still reachable and must still be advertised.
    assert Mechanisms(batching=False).capabilities() == list(CAPABILITY_METHODS)
