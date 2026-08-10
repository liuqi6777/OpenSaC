from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from opensac_sdk.models import ContentSnippet, SearchBatch, SearchHit

from opensac.broker.policy import BudgetExceeded, MechanismDisabled
from opensac.broker.service import BrokerService
from opensac.models import (
    CAPABILITY_METHODS,
    Mechanisms,
    ResourceBudget,
    RunLimits,
    Session,
)


class FakeBackend:
    """One hit per rank, so `offset` is observable rather than assumed.

    Ranks are absolute positions in the full result list -- the same contract
    the real backends keep -- which is what lets a test tell "the window moved"
    apart from "the window was renumbered".
    """

    max_depth = None

    def __init__(self, name: str, *, depth: int = 1) -> None:
        self.name = name
        self.depth = depth

    @property
    def supports_domains(self) -> bool:
        # Mirrors the real pair rather than declaring one answer, so a test
        # standing this in for "local" gets local's refusals.
        return self.name == "web"

    def _hit(self, query: str, rank: int) -> SearchHit:
        return SearchHit(
            ref="",
            backend=self.name,
            title=query,
            url=f"https://example.com/{rank}" if self.name == "web" else None,
            docid=str(rank) if self.name == "local" else None,
            snippet="snippet",
            rank=rank,
        )

    async def search(self, query, *, limit, offset=0, domains=None):
        ranks = range(offset + 1, min(offset + limit, self.depth) + 1)
        return [self._hit(query, rank) for rank in ranks]

    async def content(self, hits, *, query=None):
        return [ContentSnippet(ref=hit.ref, text=f"content:{query}", url=hit.url) for hit in hits]


class BrokenBackend:
    name = "web"
    supports_domains = True
    max_depth = None

    async def search(self, query, *, limit, offset=0, domains=None):
        raise RuntimeError("backend exploded")

    async def content(self, hits, *, query=None):
        raise RuntimeError("backend exploded")


def make_session(*, backends=None, mechanisms=None, budget=None):
    return Session(
        id="sess_test",
        token="token",
        backends=backends or ["web"],
        limits=RunLimits(),
        workspace="/tmp/session",
        mechanisms=mechanisms or Mechanisms(),
        budget=budget or ResourceBudget(),
    )


async def test_broker_scopes_references_and_fetches_content() -> None:
    service = BrokerService({"web": FakeBackend("web")})
    service.register_session(make_session())
    hits = await service.call("token", "search.query", {"query": "query", "limit": 3})
    assert hits[0]["ref"].startswith("ref_")
    content = await service.call(
        "token",
        "content.snippets",
        {"refs": [hits[0]["ref"]], "query": "fact"},
    )
    assert content[0]["text"] == "content:fact"
    citations = await service.call("token", "citations.resolve", {"refs": [hits[0]["ref"]]})
    assert citations[0]["url"] == "https://example.com/1"


async def test_search_fails_loudly_when_the_session_backend_is_not_configured() -> None:
    """The one thing resolution must never do is pick something else.

    `search.query` carries no backend name, so a session pointed at a backend
    this broker does not have has to stop here. Falling through to whatever is
    configured would run the whole question against the wrong corpus and report
    a score for it.
    """
    service = BrokerService({"local": FakeBackend("local")})
    service.register_session(make_session(backends=["web"]))
    with pytest.raises(RuntimeError, match="exactly one configured search backend"):
        await service.call("token", "search.query", {"query": "query"})


async def test_failed_search_consumes_hard_budget_before_backend_side_effect() -> None:
    service = BrokerService({"web": BrokenBackend()})
    state = service.register_session(
        make_session(budget=ResourceBudget(max_search_queries=1))
    )

    with pytest.raises(RuntimeError, match="backend exploded"):
        await service.call(
            "token", "search.query", {"query": "first"}, execution_id="exec-1"
        )
    with pytest.raises(BudgetExceeded, match="max_search_queries"):
        await service.call(
            "token", "search.query", {"query": "retry"}, execution_id="exec-1"
        )

    assert state.policy.usage.search_calls == 1
    assert state.policy.remaining()["max_search_queries"] == 0
    assert state.policy.terminal_reason == "budget_exhausted:max_search_queries"
    trace = service.take_trace("token", "exec-1")
    assert [event.error_type for event in trace] == ["RuntimeError", "BudgetExceeded"]


async def test_concurrent_search_budget_reservations_never_overspend() -> None:
    class CountingBackend(FakeBackend):
        def __init__(self) -> None:
            super().__init__("web")
            self.calls = 0

        async def search(self, query, *, limit, offset=0, domains=None):
            self.calls += 1
            await asyncio.sleep(0)
            return await super().search(
                query, limit=limit, offset=offset, domains=domains
            )

    backend = CountingBackend()
    service = BrokerService({"web": backend})
    state = service.register_session(
        make_session(budget=ResourceBudget(max_search_queries=1))
    )

    results = await asyncio.gather(
        *(
            service.call("token", "search.query", {"query": f"query-{index}"})
            for index in range(8)
        ),
        return_exceptions=True,
    )

    assert sum(isinstance(result, list) for result in results) == 1
    assert sum(isinstance(result, BudgetExceeded) for result in results) == 7
    assert backend.calls == 1
    assert state.policy.usage.search_calls == 1


async def test_search_refuses_a_domain_filter_the_backend_cannot_honour() -> None:
    """Silently dropping it is how a program mistakes the whole web for one site.

    It filters by domain, gets unfiltered hits back, finds nothing relevant and
    concludes the site has no such page -- with no signal anywhere that the
    constraint was never applied. This is the reason the method name is
    backend-neutral while the parameters are not.
    """
    service = BrokerService({"local": FakeBackend("local")})
    service.register_session(make_session(backends=["local"]))
    with pytest.raises(ValueError, match="no domain filter"):
        await service.call(
            "token", "search.query", {"query": "q", "domains": ["example.com"]}
        )
    # The same call without the argument is fine, so the refusal is about the
    # parameter rather than about the backend.
    assert await service.call("token", "search.query", {"query": "q"})


async def test_search_refuses_depth_the_backend_cannot_serve() -> None:
    """Enforced centrally so every backend refuses in the same words."""

    class Shallow(FakeBackend):
        max_depth = 100

    service = BrokerService({"web": Shallow("web", depth=200)})
    service.register_session(make_session(backends=["web"]))
    with pytest.raises(ValueError, match="reaches rank 100 at most"):
        await service.call("token", "search.query", {"query": "q", "limit": 10, "offset": 100})


async def test_search_rejects_query_and_depth_budgets_before_backend_call() -> None:
    class Counting(FakeBackend):
        def __init__(self) -> None:
            super().__init__("web", depth=100)
            self.calls = 0

        async def search(self, query, *, limit, offset=0, domains=None):
            self.calls += 1
            return await super().search(
                query, limit=limit, offset=offset, domains=domains
            )

    backend = Counting()
    service = BrokerService(
        {"web": backend},
        max_search_query_chars=4,
        max_search_top_k=20,
    )
    service.register_session(make_session())

    with pytest.raises(ValueError, match="5 characters"):
        await service.call("token", "search.query", {"query": "abcde"})
    with pytest.raises(ValueError, match="retrieval depth 21"):
        await service.call(
            "token", "search.query", {"query": "ok", "limit": 10, "offset": 11}
        )

    assert backend.calls == 0


async def test_search_many_rejects_hard_budgets_before_fanout() -> None:
    service = BrokerService(
        {"web": FakeBackend("web")},
        max_search_queries_per_request=2,
        max_search_query_chars=4,
        max_search_top_k=20,
    )
    state = service.register_session(make_session())

    with pytest.raises(ValueError, match="3 queries"):
        await service.call(
            "token",
            "search.query_many",
            {"queries": ["a", "b", "c"]},
            execution_id="oversized-batch",
        )
    with pytest.raises(ValueError, match="index 1 has 5 characters"):
        await service.call(
            "token", "search.query_many", {"queries": ["ok", "abcde"]}
        )
    with pytest.raises(ValueError, match="retrieval depth 21"):
        await service.call(
            "token",
            "search.query_many",
            {"queries": ["ok"], "limit_per_query": 10, "offset": 11},
        )

    assert state.policy.usage.search_calls == 0
    rejected = service.take_trace("token", "oversized-batch")[0]
    assert rejected.input_count == 3
    assert rejected.queries == ["a", "b"]


async def test_searches_are_counted_and_never_capped() -> None:
    """Retrieval volume across valid calls is measured, not rationed.

    A ceiling here has two fates and neither helps: high enough not to
    interfere it is dead code, low enough to bind it turns a question into a
    zero that afterwards reads as a model failure. Volume is also the quantity
    a paradigm comparison is trying to observe, so fixing it would remove the
    measurement. Per-request shape limits only reject pathological individual
    payloads; they do not impose a rollout-level search-call budget.
    """
    service = BrokerService({"web": FakeBackend("web")})
    state = service.register_session(make_session())
    for index in range(25):
        await service.call("token", "search.query", {"query": f"q{index}"})
    assert state.policy.usage.search_calls == 25


async def test_search_many_raises_when_every_query_fails() -> None:
    service = BrokerService({"web": BrokenBackend()})
    service.register_session(make_session())
    with pytest.raises(RuntimeError, match="backend exploded"):
        await service.call("token", "search.query_many", {"queries": ["one", "two"]})


async def test_search_many_tolerates_partial_failure() -> None:
    service = BrokerService({"web": FakeBackend("web")})
    service.register_session(make_session())
    # An empty query is rejected by the broker while the other one succeeds.
    batches = await service.call("token", "search.query_many", {"queries": ["ok", ""]})
    assert len(batches[0]["hits"]) == 1
    assert batches[0]["error"] is None
    assert batches[1]["hits"] == []
    assert "must not be empty" in batches[1]["error"]


async def test_local_search_many_prefers_backend_batch_and_preserves_order() -> None:
    class BatchLocal(FakeBackend):
        def __init__(self) -> None:
            super().__init__("local")
            self.batch_calls: list[list[str]] = []
            self.single_calls = 0

        async def search(self, query, *, limit, offset=0, domains=None):
            self.single_calls += 1
            return await super().search(
                query, limit=limit, offset=offset, domains=domains
            )

        async def search_many(self, queries, *, limit, offset=0, domains=None):
            self.batch_calls.append(list(queries))
            return [
                SearchBatch(query=query, hits=[self._hit(query, offset + 1)])
                for query in queries
            ]

    backend = BatchLocal()
    service = BrokerService({"local": backend})
    state = service.register_session(make_session(backends=["local"]))

    batches = await service.call(
        "token",
        "search.query_many",
        {"queries": ["second", "", "first"], "limit_per_query": 1},
    )

    assert backend.batch_calls == [["second", "first"]]
    assert backend.single_calls == 0
    assert [batch["query"] for batch in batches] == ["second", "", "first"]
    assert [batches[0]["hits"][0]["title"], batches[2]["hits"][0]["title"]] == [
        "second",
        "first",
    ]
    assert "must not be empty" in batches[1]["error"]
    assert state.policy.usage.search_calls == 3


async def test_broker_closes_each_backend_instance_once() -> None:
    class Closable(FakeBackend):
        def __init__(self) -> None:
            super().__init__("local")
            self.close_calls = 0

        async def aclose(self) -> None:
            self.close_calls += 1

    backend = Closable()
    service = BrokerService({"local": backend, "same-instance": backend})

    await service.aclose()

    assert backend.close_calls == 1


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


def make_llm_service() -> tuple[BrokerService, FakeModelClient]:
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
            limits=RunLimits(),
            workspace="/tmp/session",
        )
    )
    return service, client


async def test_llm_complete_passes_system_prompt_and_charges_one_call() -> None:
    service, client = make_llm_service()
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


async def test_pipeline_output_budget_clamps_and_reserves_before_call() -> None:
    client = FakeModelClient()
    service = BrokerService(
        {"web": FakeBackend("web")},
        model_client=client,
        extraction_model="test-model",
    )
    state = service.register_session(
        make_session(budget=ResourceBudget(max_pipeline_output_tokens=5))
    )

    await service.call(
        "token",
        "llm.complete",
        {"prompt": "bounded", "max_tokens": 100},
    )

    assert client.calls[0]["max_completion_tokens"] == 5
    assert state.policy.usage.pipeline_output_tokens_reserved == 5
    assert state.policy.usage.pipeline_model_tokens == 11
    with pytest.raises(BudgetExceeded, match="max_pipeline_output_tokens"):
        await service.call("token", "llm.complete", {"prompt": "again"})


async def test_capability_trace_records_compact_inputs_results_and_errors() -> None:
    service = BrokerService({"web": FakeBackend("web")})
    service.register_session(make_session())
    await service.call(
        "token",
        "search.query_many",
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
    assert [event.method for event in trace] == ["search.query_many", "content.get_many"]
    # Also pins the `_many` suffix. `_trace_queries` and `_trace_input_count`
    # split on it, and the host's analysis derives queries-per-question from
    # this field; a batch method renamed without the suffix would leave both
    # counting one query per call and reporting a plausible wrong number.
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
    hits = await service.call("token", "search.query", {"query": "seed"})
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
    service, _ = make_llm_service()
    answers = await service.call(
        "token",
        "llm.complete_many",
        {"prompts": ["one", "two", "three"], "concurrency": 3},
        execution_id="exec-many",
    )
    assert answers == ["echo:one", "echo:two", "echo:three"]
    assert service.sessions["token"].policy.usage.llm_calls == 3
    assert service.take_trace("token", "exec-many")[0].model_tokens == 33


async def test_a_fanout_is_counted_at_the_size_it_was_dispatched() -> None:
    """A batch that dies partway is still reported at its full width."""
    service, _ = make_llm_service()
    await service.call("token", "llm.complete_many", {"prompts": ["one", "two", "three"]})
    assert service.sessions["token"].policy.usage.llm_calls == 3


async def test_llm_calls_fail_when_no_model_is_configured() -> None:
    service = BrokerService({"web": FakeBackend("web")})
    service.register_session(make_session())
    with pytest.raises(RuntimeError, match="not configured"):
        await service.call("token", "llm.complete", {"prompt": "hello"})


class RankedBackend:
    """Returns a fixed document set, so the same page recurs across queries."""

    name = "web"

    supports_domains = True

    max_depth = None

    def __init__(self, urls: list[str]) -> None:
        self.urls = urls

    async def search(self, query, *, limit, offset=0, domains=None):
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
            for index, url in enumerate(self.urls[: offset + limit])
            if index >= offset
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
    state = service.register_session(make_session())

    first = await service.call("token", "search.query", {"query": "one"})
    second = await service.call("token", "search.query", {"query": "two"})

    assert first[0]["ref"] == second[0]["ref"]
    assert len(state.references) == 1


async def test_refs_are_opaque_and_reproducible() -> None:
    """Same document, different process: same handle."""
    urls = ["https://Example.com/a?utm_source=news&id=7#section"]
    left = BrokerService({"web": RankedBackend(urls)})
    left.register_session(make_session())
    right = BrokerService({"web": RankedBackend(urls)})
    right.register_session(make_session())

    one = (await left.call("token", "search.query", {"query": "q"}))[0]["ref"]
    two = (await right.call("token", "search.query", {"query": "q"}))[0]["ref"]

    assert one == two
    assert one.startswith("ref_")
    # Opaque: nothing a program could have constructed for itself.
    assert "example.com" not in one


async def test_a_docid_reaches_the_document_a_ref_reaches() -> None:
    """The handle the model can actually re-type must work.

    A ref has to be unguessable, which makes it long and random-looking, and a
    program carries it across turns by copying it through its own output. The
    docid is right there in the same hit and is what a model reaches for. Both
    now resolve to one document, so the design keeps the property it needs
    (unforgeable) without charging for one it does not (verbatim transcription).
    """
    service = BrokerService({"local": FakeBackend("local")})
    service.register_session(make_session(backends=["local"]))
    hits = await service.call("token", "search.query", {"query": "q"})

    by_ref = await service.call("token", "content.get_many", {"refs": [hits[0]["ref"]]})
    by_docid = await service.call("token", "content.get_many", {"refs": [hits[0]["docid"]]})
    by_ref_again = await service.call("token", "citations.resolve", {"refs": [hits[0]["docid"]]})

    assert by_docid == by_ref
    # A citation resolved from a docid still reports the canonical ref, so
    # provenance does not fork by which key the caller happened to use.
    assert by_ref_again[0]["ref"] == hits[0]["ref"]


async def test_a_document_this_session_never_searched_is_still_refused() -> None:
    """The admission rule is unchanged; only the lookup key is wider.

    `_resolve_refs` raising is the enforcement point of the capability
    boundary, not parameter validation: the sandbox has no network, so search
    is the only door. If a docid the corpus contains were reachable without
    being retrieved, a program could walk the docid space and any recall
    measurement over it would be meaningless.
    """
    service = BrokerService({"local": FakeBackend("local")})
    service.register_session(make_session(backends=["local"]))
    await service.call("token", "search.query", {"query": "q"})

    with pytest.raises(ValueError, match="Unknown references"):
        await service.call("token", "content.get_many", {"refs": ["999"]})


async def test_offset_reaches_ranks_a_bare_limit_cannot() -> None:
    """Depth is authorisation, not convenience.

    Because a ref is minted only for a returned hit, `limit` is both what a
    program can see and what it is allowed to fetch. Without an offset, a
    document at rank 15 is not merely inconvenient to reach, it is unreachable.
    """
    service = BrokerService({"local": FakeBackend("local", depth=50)})
    state = service.register_session(make_session(backends=["local"]))

    shallow = await service.call("token", "search.query", {"query": "q", "limit": 10})
    deep = await service.call(
        "token", "search.query", {"query": "q", "limit": 10, "offset": 10}
    )

    assert [hit["rank"] for hit in shallow] == list(range(1, 11))
    # Ranks stay absolute: the second window reports 11..20, not 1..10 again.
    assert [hit["rank"] for hit in deep] == list(range(11, 21))
    assert not {hit["ref"] for hit in shallow} & {hit["ref"] for hit in deep}
    # And the deeper hits are now fetchable, which is the point.
    assert deep[0]["docid"] in state.by_docid


async def test_grep_line_numbers_are_read_offsets() -> None:
    """The two halves compose without character arithmetic.

    This is the same contract the function-calling profiles keep, and matching
    it is deliberate: it is the coordinate system the model already writes
    against, and a second convention here would be paid for in wrong offsets.
    """
    lines = [f"line {index}" for index in range(1, 41)]
    lines[24] = "the target phrase is here"

    class Paged:
        name = "local"
        supports_domains = False
        max_depth = None

        async def search(self, query, *, limit, offset=0, domains=None):
            return [SearchHit(ref="", backend="local", docid="d1", snippet="s", rank=1)]

        async def content(self, hits, *, query=None):
            return [ContentSnippet(ref=hit.ref, text="\n".join(lines)) for hit in hits]

    service = BrokerService({"local": Paged()})
    service.register_session(make_session(backends=["local"]))
    hits = await service.call("token", "search.query", {"query": "q"})
    ref = hits[0]["ref"]

    matches = await service.call(
        "token", "content.grep", {"refs": [ref], "pattern": r"target \w+", "context": 1}
    )
    assert len(matches) == 1
    assert matches[0]["line"] == 25
    assert matches[0]["before"] == ["line 24"]
    assert matches[0]["after"] == ["line 26"]

    window = await service.call(
        "token", "content.read", {"refs": [ref], "offset": matches[0]["line"], "limit": 2}
    )
    assert window[0]["text"].splitlines()[0] == "the target phrase is here"
    assert window[0]["metadata"]["start_line"] == 25
    assert window[0]["metadata"]["total_lines"] == 40


async def test_read_reports_where_to_continue_and_where_to_stop() -> None:
    """`next_offset` is None at the end, so `while offset:` terminates."""

    class Doc:
        name = "local"
        supports_domains = False
        max_depth = None

        async def search(self, query, *, limit, offset=0, domains=None):
            return [SearchHit(ref="", backend="local", docid="d1", snippet="s", rank=1)]

        async def content(self, hits, *, query=None):
            return [ContentSnippet(ref=hit.ref, text="\n".join("abcde")) for hit in hits]

    service = BrokerService({"local": Doc()})
    service.register_session(make_session(backends=["local"]))
    ref = (await service.call("token", "search.query", {"query": "q"}))[0]["ref"]

    head = await service.call("token", "content.read", {"refs": [ref], "limit": 3})
    assert head[0]["text"] == "a\nb\nc"
    assert head[0]["metadata"]["next_offset"] == 4

    tail = await service.call(
        "token", "content.read", {"refs": [ref], "offset": 4, "limit": 3}
    )
    assert tail[0]["text"] == "d\ne"
    assert tail[0]["metadata"]["next_offset"] is None


async def test_grep_falls_back_to_a_literal_search_for_a_bad_pattern() -> None:
    """A program that meant `C++ (lang)` should get matches, not a traceback."""

    class Doc:
        name = "local"
        supports_domains = False
        max_depth = None

        async def search(self, query, *, limit, offset=0, domains=None):
            return [SearchHit(ref="", backend="local", docid="d1", snippet="s", rank=1)]

        async def content(self, hits, *, query=None):
            return [ContentSnippet(ref=hit.ref, text="written in C++ (1985)") for hit in hits]

    service = BrokerService({"local": Doc()})
    service.register_session(make_session(backends=["local"]))
    ref = (await service.call("token", "search.query", {"query": "q"}))[0]["ref"]

    matches = await service.call("token", "content.grep", {"refs": [ref], "pattern": "C++ ("})
    assert [match["line"] for match in matches] == [1]


class CountingBackend:
    """Records how often it was actually asked to retrieve a document."""

    name = "local"

    supports_domains = False

    max_depth = None

    def __init__(self, *, fail: set[str] | None = None) -> None:
        self.fetched: list[str] = []
        self.fail = fail or set()

    async def search(self, query, *, limit, offset=0, domains=None):
        return [
            SearchHit(ref="", backend="local", docid=str(index), snippet="s", rank=index)
            for index in range(offset + 1, offset + limit + 1)
        ]

    async def content(self, hits, *, query=None):
        rows = []
        for hit in hits:
            self.fetched.append(hit.docid)
            if hit.docid in self.fail:
                rows.append(
                    ContentSnippet(
                        ref=hit.ref,
                        text="",
                        metadata={"docid": hit.docid, "fetch_error": "HTTPError: 403"},
                    )
                )
            else:
                rows.append(
                    ContentSnippet(
                        ref=hit.ref,
                        text=f"body of {hit.docid}",
                        metadata={"docid": hit.docid},
                    )
                )
        return rows


async def test_a_document_is_retrieved_once_per_session() -> None:
    """grep and read are meant to be used repeatedly over one pool.

    Without a cache the recommended survey/locate/verify shape refetches every
    candidate once per stage. Against a local index that is merely wasteful;
    against a metered scrape API it is three times the bill and the latency.
    """
    backend = CountingBackend()
    service = BrokerService({"local": backend})
    state = service.register_session(make_session(backends=["local"]))
    hits = await service.call("token", "search.query", {"query": "q", "limit": 3})
    refs = [hit["ref"] for hit in hits]

    await service.call("token", "content.get_many", {"refs": refs})
    await service.call("token", "content.grep", {"refs": refs, "pattern": "body"})
    await service.call("token", "content.read", {"refs": refs})

    assert backend.fetched == ["1", "2", "3"]
    # Both numbers are reported: one follows the program's behaviour, the other
    # follows the bill, and a cache is exactly what makes them diverge.
    assert state.policy.usage.content_fetches == 9
    assert state.policy.usage.content_backend_fetches == 3


async def test_a_selected_passage_never_overwrites_the_cached_document() -> None:
    """`content.snippets` rewrites the rows it is handed.

    If those rows were the cached objects, one call to `snippets` would replace
    the stored document with the passage it chose, and every later `read` of
    that document would silently be a read of that passage instead.
    """
    backend = CountingBackend()
    service = BrokerService({"local": backend})
    service.register_session(make_session(backends=["local"]))
    ref = (await service.call("token", "search.query", {"query": "q", "limit": 1}))[0]["ref"]

    await service.call(
        "token", "content.snippets", {"refs": [ref], "query": "body", "max_tokens_per_page": 1}
    )
    rows = await service.call("token", "content.get_many", {"refs": [ref]})

    assert rows[0]["text"] == "body of 1"


async def test_every_requested_document_comes_back_in_order() -> None:
    """A short list is never mistaken for a complete one.

    A dropped failure makes a partial result look whole: the program sees two
    pages where it asked for three and cannot learn which one is missing, and
    `read` on a page that failed to load becomes indistinguishable from `read`
    on a page that is empty.
    """
    backend = CountingBackend(fail={"2"})
    service = BrokerService({"local": backend})
    service.register_session(make_session(backends=["local"]))
    hits = await service.call("token", "search.query", {"query": "q", "limit": 3})
    refs = [hit["ref"] for hit in hits]

    rows = await service.call("token", "content.get_many", {"refs": refs})

    assert [row["ref"] for row in rows] == refs
    assert rows[1]["metadata"]["fetch_error"] == "HTTPError: 403"
    assert rows[1]["text"] == ""
    # A failure is not cached: a transient timeout must not be frozen for the
    # rest of the rollout.
    await service.call("token", "content.get_many", {"refs": refs})
    assert backend.fetched == ["1", "2", "3", "2"]


async def test_every_fetch_failing_is_raised_not_reported_as_empty_pages() -> None:
    backend = CountingBackend(fail={"1", "2"})
    service = BrokerService({"local": backend})
    service.register_session(make_session(backends=["local"]))
    hits = await service.call("token", "search.query", {"query": "q", "limit": 2})

    with pytest.raises(RuntimeError, match="All 2 document fetches failed"):
        await service.call(
            "token", "content.get_many", {"refs": [hit["ref"] for hit in hits]}
        )


async def test_read_is_bounded_by_characters_as_well_as_lines() -> None:
    """A line is a sentence in one corpus and a whole section in another."""

    class Fat:
        name = "local"
        supports_domains = False
        max_depth = None

        async def search(self, query, *, limit, offset=0, domains=None):
            return [SearchHit(ref="", backend="local", docid="d1", snippet="s", rank=1)]

        async def content(self, hits, *, query=None):
            return [ContentSnippet(ref=hit.ref, text="\n".join(["x" * 500] * 20)) for hit in hits]

    service = BrokerService({"local": Fat()})
    service.register_session(make_session(backends=["local"]))
    ref = (await service.call("token", "search.query", {"query": "q"}))[0]["ref"]

    rows = await service.call(
        "token", "content.read", {"refs": [ref], "limit": 20, "max_chars": 1200}
    )
    assert len(rows[0]["text"]) <= 1200
    assert rows[0]["metadata"]["truncated_by_max_chars"] is True
    # Trimmed on a line boundary, so the reported end_line is a real one and a
    # follow-up read resumes where this one stopped.
    assert rows[0]["metadata"]["end_line"] == 2
    assert rows[0]["metadata"]["next_offset"] == 3


async def test_a_program_can_read_what_it_has_spent() -> None:
    """The counts, so a program can ration itself.

    The broker imposes no ceiling, which is what makes this necessary rather
    than merely informative: a policy the program applies is visible in its
    code and can be measured, and one the broker imposes can only be hit.
    """
    service = BrokerService({"local": FakeBackend("local", depth=5)})
    service.register_session(make_session(backends=["local"]))
    await service.call("token", "search.query", {"query": "q", "limit": 2})
    await service.call("token", "content.get_many", {"refs": ["1"]})

    usage = await service.call("token", "session.usage", {})

    assert usage["search_calls"] == 1
    assert usage["content_fetches"] == 1
    assert usage["documents_seen"] == 2
    assert "max_search_calls" not in usage


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
    service.register_session(make_session())

    await service.call(
        "token",
        "search.query_many",
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


async def test_trace_records_which_documents_a_content_call_opened() -> None:
    """Surfacing and opening are different failures with different remedies.

    Searches have always carried identities, so a trace could say whether the
    gold document was ever *found*. Without the same on content events it could
    not say whether it was ever *read* -- and "the query never reached it" and
    "the program saw it and moved on" call for opposite fixes. Nothing
    reconstructs it afterwards: the handle in the transcript is opaque and the
    table behind it dies with the session.
    """
    service = BrokerService({"local": FakeBackend("local", depth=3)})
    service.register_session(make_session(backends=["local"]))
    await service.call("token", "search.query", {"query": "q", "limit": 3})

    await service.call(
        "token",
        "content.get_many",
        {"refs": ["2", "3"]},
        execution_id="exec-read",
    )
    event = service.take_trace("token", "exec-read")[0]

    assert [hit.identity for hit in event.hits] == ["local:docid:2", "local:docid:3"]
    # The rank of the sighting that put the document in reach, not of this call.
    assert [hit.rank for hit in event.hits] == [2, 3]
    assert "content:" not in event.model_dump_json()


async def test_trace_records_which_documents_a_citation_resolved() -> None:
    """`citations.resolve` takes handles too, so provenance is traceable the same way."""
    service = BrokerService({"local": FakeBackend("local", depth=2)})
    service.register_session(make_session(backends=["local"]))
    await service.call("token", "search.query", {"query": "q", "limit": 2})

    await service.call(
        "token",
        "citations.resolve",
        {"refs": ["1"]},
        execution_id="exec-cite",
    )
    event = service.take_trace("token", "exec-cite")[0]

    assert [hit.identity for hit in event.hits] == ["local:docid:1"]


async def test_a_failed_event_records_why_and_not_only_the_type() -> None:
    """`error_type` alone collapses cases whose remedies differ.

    Every one of these is a `ValueError`: an unknown handle means the program
    lost a reference, an empty pattern means it built the call wrong. A trace
    that stored only the type would report them as one failure mode.
    """
    service = BrokerService({"local": FakeBackend("local", depth=2)})
    service.register_session(make_session(backends=["local"]))
    await service.call("token", "search.query", {"query": "q", "limit": 2})

    with pytest.raises(ValueError):
        await service.call(
            "token",
            "content.get_many",
            {"refs": ["nope"]},
            execution_id="exec-unknown",
        )
    with pytest.raises(ValueError):
        await service.call(
            "token",
            "content.grep",
            {"refs": ["1"], "pattern": ""},
            execution_id="exec-pattern",
        )

    unknown = service.take_trace("token", "exec-unknown")[0]
    pattern = service.take_trace("token", "exec-pattern")[0]

    assert unknown.error_type == pattern.error_type == "ValueError"
    assert "Unknown references: nope" in (unknown.error or "")
    assert "pattern must not be empty" in (pattern.error or "")
    # A call that resolved nothing opened nothing.
    assert unknown.hits == []


def test_a_trace_error_message_is_bounded_but_never_empty() -> None:
    """A backend is free to put a response body in an exception.

    Bounded for volume rather than secrecy -- the trace already records
    addresses and queries verbatim -- and `None` rather than `""` for a bare
    raise, because an empty string reads as "the message was dropped".
    """
    bound = BrokerService._ERROR_MESSAGE_CHARS
    long = BrokerService._trace_error_message(RuntimeError("x" * (bound + 100)))

    assert long is not None
    assert long.startswith("x" * 32) and long.endswith("... [truncated]")
    assert len(long) < bound + 100
    assert BrokerService._trace_error_message(ValueError()) is None



async def test_batching_disabled_forces_one_item_per_call() -> None:
    """The switch bounds fan-out; it does not remove the method.

    Removing `*_many` outright would also remove structured extraction, since
    `llm.extract_many` has no singular form, and the arm would then be measuring
    two things at once.
    """
    service = BrokerService({"web": RankedBackend(["https://example.com/a"])})
    service.register_session(
        make_session(mechanisms=Mechanisms(batching=False))
    )

    with pytest.raises(MechanismDisabled, match="at most one item"):
        await service.call(
            "token",
            "search.query_many",
            {"queries": ["one", "two"]},
            execution_id="exec-block",
        )
    batches = await service.call("token", "search.query_many", {"queries": ["one"]})
    assert len(batches[0]["hits"]) == 1

    # A blocked call is still an event: an arm that disables a capability wants
    # to know how often the model kept reaching for it.
    blocked = service.take_trace("token", "exec-block")[0]
    assert blocked.status == "error"
    assert blocked.error_type == "MechanismDisabled"


async def test_llm_subroutine_disabled_blocks_the_whole_capability_class() -> None:
    service, client = make_llm_service()
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
        "token", "search.query", {"query": "q"}, execution_id="exec-echo"
    )
    event = service.take_trace("token", "exec-echo")[0]
    assert event.result_payload == hits
    assert event.result_payload_truncated is False


async def test_default_sessions_keep_results_out_of_the_trace() -> None:
    service = BrokerService({"web": RankedBackend(["https://example.com/a"])})
    service.register_session(make_session())
    await service.call("token", "search.query", {"query": "q"}, execution_id="exec-plain")
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
        "token", "search.query", {"query": "q", "limit": 50}, execution_id="exec-big"
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
    assert "search.query_many" in without_llm
    # Batching bounds a call's width rather than removing it, so the method is
    # still reachable and must still be advertised.
    assert Mechanisms(batching=False).capabilities() == list(CAPABILITY_METHODS)


async def test_a_ref_with_the_prefix_dropped_still_resolves() -> None:
    """The one spelling repair allowed, because it is not a guess.

    Restoring a constant prefix is an exact hit on a full key: it resolves to
    one document or to none. Programs lose the prefix by slicing their own
    output or reading a truncated column, and the fix costs nothing.
    """
    service = BrokerService({"local": FakeBackend("local")})
    service.register_session(make_session(backends=["local"]))
    hits = await service.call("token", "search.query", {"query": "q"})
    ref = hits[0]["ref"]

    bare = await service.call("token", "content.get_many", {"refs": [ref.removeprefix("ref_")]})
    assert bare == await service.call("token", "content.get_many", {"refs": [ref]})


async def test_a_mistyped_ref_is_refused_rather_than_repaired() -> None:
    """Nearest-match would nearly always be right, which is why it is absent.

    A few hundred refs in a 16-hex space means an edit-distance-1 neighbour is
    almost certainly the intended document -- and "almost certainly" about
    *which document* is the wrong trade. It converts an error the program can
    see into a silent read of, and citation to, a document nobody asked for.
    """
    service = BrokerService({"local": FakeBackend("local")})
    service.register_session(make_session(backends=["local"]))
    hits = await service.call("token", "search.query", {"query": "q"})
    ref = hits[0]["ref"]

    one_character_off = ref[:-1] + ("0" if ref[-1] != "0" else "1")
    with pytest.raises(ValueError, match="Unknown references"):
        await service.call("token", "content.get_many", {"refs": [one_character_off]})
    # A truncated ref is a prefix of a real one and is refused for the same
    # reason: a prefix that is unique today can be ambiguous later.
    with pytest.raises(ValueError, match="Unknown references"):
        await service.call("token", "content.get_many", {"refs": [ref[:-2]]})


async def test_a_single_handle_passed_unwrapped_is_not_read_character_by_character() -> None:
    """A bare string is iterable, so the error it produced named nothing.

    `Unknown references: r, e, f` tells a program neither what was wrong nor
    which handle failed. Accepting the unwrapped form resolves the same
    document the wrapped form would, so nothing new becomes reachable.
    """
    service = BrokerService({"local": FakeBackend("local")})
    service.register_session(make_session(backends=["local"]))
    hits = await service.call("token", "search.query", {"query": "q"})

    unwrapped = await service.call("token", "content.get_many", {"refs": hits[0]["ref"]})
    assert unwrapped == await service.call("token", "content.get_many", {"refs": [hits[0]["ref"]]})


async def test_a_snippet_carries_the_date_of_the_hit_that_found_it() -> None:
    """Filled from the hit, so a backend cannot forget to."""
    service = BrokerService({"local": FakeBackend("local")})
    service.register_session(make_session(backends=["local"]))
    hits = await service.call("token", "search.query", {"query": "q"})
    rows = await service.call("token", "content.get_many", {"refs": [hits[0]["ref"]]})
    assert rows[0].get("date") == hits[0].get("date")
