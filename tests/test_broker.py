from __future__ import annotations

import asyncio
import json
from collections import Counter
from types import SimpleNamespace

import pytest
from opensac_sdk._surface import BROKER_METHODS
from opensac_sdk.models import ContentSnippet, RetrievalMetadata, SearchBatch, SearchHit

from opensac.broker.policy import BudgetExceeded, MechanismDisabled
from opensac.broker.service import (
    BrokerService,
    CapabilityProviderError,
    ExtractionInfrastructureError,
    InflightCapacityError,
)
from opensac.models import (
    CAPABILITY_METHODS,
    Mechanisms,
    ResourceBudget,
    Session,
)
from opensac.provider import ProviderRequestError
from opensac.rerankers import JinaPassageReranker, PassageRerankResult


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


class PassageCorpusBackend:
    """Frozen in-memory web pages with observable per-document fetches."""

    name = "web"
    supports_domains = True
    max_depth = 100

    def __init__(self, documents: list[str], *, fail: set[int] | None = None) -> None:
        self.documents = documents
        self.fail = fail or set()
        self.fetched: list[int] = []

    async def search(self, query, *, limit, offset=0, domains=None):
        del query, domains
        return [
            SearchHit(
                ref="",
                backend="web",
                title=f"Frozen page {index}",
                url=f"https://example.test/{index}",
                date=f"202{index}",
                snippet="frozen",
                rank=index + 1,
            )
            for index in range(offset, min(offset + limit, len(self.documents)))
        ]

    async def content(self, hits, *, query=None):
        del query
        rows = []
        for hit in hits:
            index = int(str(hit.url).rsplit("/", 1)[-1])
            self.fetched.append(index)
            if index in self.fail:
                rows.append(
                    ContentSnippet(
                        ref=hit.ref,
                        text="",
                        title=hit.title,
                        url=hit.url,
                        metadata={"fetch_error": "secret upstream response"},
                    )
                )
            else:
                rows.append(
                    ContentSnippet(
                        ref=hit.ref,
                        text=self.documents[index],
                        title=hit.title,
                        url=hit.url,
                        date=hit.date,
                    )
                )
        return rows


def make_session(*, backends=None, mechanisms=None, budget=None):
    return Session(
        id="sess_test",
        token="token",
        backends=backends or ["web"],
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
        "content.read",
        {"refs": [hits[0]["ref"]]},
    )
    assert content[0]["text"] == "content:None"
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

    with pytest.raises(ProviderRequestError) as failed:
        await service.call(
            "token", "search.query", {"query": "first"}, execution_id="exec-1"
        )
    assert failed.value.code == "provider_invalid_response"
    assert failed.value.attempts == 1
    with pytest.raises(BudgetExceeded, match="max_search_queries"):
        await service.call(
            "token", "search.query", {"query": "retry"}, execution_id="exec-1"
        )

    assert state.policy.usage.search_calls == 1
    assert state.policy.remaining()["max_search_queries"] == 0
    assert state.policy.terminal_reason == "budget_exhausted:max_search_queries"
    trace = service.take_trace("token", "exec-1")
    assert [event.error_type for event in trace] == [
        "ProviderRequestError",
        "BudgetExceeded",
    ]


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
    state = service.register_session(make_session())

    with pytest.raises(ValueError, match="5 characters"):
        await service.call("token", "search.query", {"query": "abcde"})
    with pytest.raises(ValueError, match="retrieval depth 21"):
        await service.call(
            "token", "search.query", {"query": "ok", "limit": 10, "offset": 11}
        )
    with pytest.raises(ValueError, match="domains must be a list"):
        await service.call(
            "token", "search.query", {"query": "ok", "domains": "example.com"}
        )

    assert backend.calls == 0
    assert state.policy.usage.search_calls == 0


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
    batches = await service.call(
        "token", "search.query_many", {"queries": ["ok", "abcde"]}
    )
    assert batches[0]["failure"] is None
    assert batches[1]["failure"]["code"] == "invalid_request"
    assert batches[1]["failure"]["attempts"] == 0
    with pytest.raises(ValueError, match="retrieval depth 21"):
        await service.call(
            "token",
            "search.query_many",
            {"queries": ["ok"], "limit_per_query": 10, "offset": 11},
        )

    assert state.policy.usage.search_calls == 2
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
    with pytest.raises(CapabilityProviderError) as failed:
        await service.call("token", "search.query_many", {"queries": ["one", "two"]})
    assert failed.value.code == "provider_invalid_response"
    assert failed.value.attempts == 2


async def test_search_many_tolerates_partial_failure() -> None:
    service = BrokerService({"web": FakeBackend("web")})
    service.register_session(make_session())
    # An empty query is rejected by the broker while the other one succeeds.
    batches = await service.call("token", "search.query_many", {"queries": ["ok", ""]})
    assert len(batches[0]["hits"]) == 1
    assert batches[0]["failure"] is None
    assert batches[1]["hits"] == []
    assert "must not be empty" in batches[1]["failure"]["message"]
    assert batches[1]["failure"] == {
        "code": "invalid_request",
        "message": "query must not be empty",
        "retryable": False,
        "attempts": 0,
        "provider_status": None,
        "retry_after_seconds": None,
    }


async def test_web_search_many_registers_provenance_in_input_order() -> None:
    class CompletionOrderBackend(FakeBackend):
        async def search(self, query, *, limit, offset=0, domains=None):
            if query == "first":
                await asyncio.sleep(0.02)
            return [
                SearchHit(
                    ref="",
                    backend="web",
                    title=query,
                    url="https://example.com/shared",
                    snippet=query,
                    rank=1,
                    retrieval=RetrievalMetadata(mode="organic", result_mode="snippet"),
                )
            ]

    service = BrokerService({"web": CompletionOrderBackend("web")})
    state = service.register_session(make_session())

    batches = await service.call(
        "token",
        "search.query_many",
        {"queries": ["first", "second"], "concurrency": 2},
        execution_id="exec-query-order",
    )

    assert batches[0]["hits"][0]["ref"] == batches[1]["hits"][0]["ref"]
    assert state.references[batches[0]["hits"][0]["ref"]].title == "first"
    event = service.take_trace("token", "exec-query-order")[0]
    assert [hit.query_index for hit in event.hits] == [0, 1]
    assert [hit.retrieval_mode for hit in event.hits] == ["organic", "organic"]


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
    assert "must not be empty" in batches[1]["failure"]["message"]
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


class ScriptedModelClient:
    """Return one scripted provider outcome per call, in dispatch order."""

    def __init__(self, outcomes: list[str | BaseException], *, tokens: int = 7) -> None:
        self.outcomes = list(outcomes)
        self.tokens = tokens
        self.calls: list[dict] = []
        parent = self

        class Completions:
            async def create(self, **kwargs):
                parent.calls.append(kwargs)
                outcome = parent.outcomes.pop(0)
                if isinstance(outcome, BaseException):
                    raise outcome
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content=outcome))],
                    usage=SimpleNamespace(total_tokens=parent.tokens),
                )

        self.chat = SimpleNamespace(completions=Completions())


def make_scripted_llm_service(
    outcomes: list[str | BaseException],
    *,
    budget: ResourceBudget | None = None,
    **limits,
) -> tuple[BrokerService, ScriptedModelClient]:
    client = ScriptedModelClient(outcomes)
    service = BrokerService(
        {"web": FakeBackend("web")},
        model_client=client,
        extraction_model="test-model",
        **limits,
    )
    service.register_session(make_session(budget=budget))
    return service, client


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
    assert state.policy.usage.llm_calls == 1


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


@pytest.mark.parametrize(
    "params",
    [
        {"query": "", "refs": []},
        {"query": None, "refs": []},
        {"query": "q", "refs": [], "limit": 0},
        {"query": "q", "refs": [], "limit": "invalid"},
        {"query": "q", "refs": [], "limit": True},
        {"query": "q", "refs": [], "limit": 101},
        {"query": "q", "refs": [], "max_per_ref": 0},
        {"query": "q", "refs": [], "max_per_ref": None},
        {"query": "q", "refs": [], "max_per_ref": 11},
    ],
)
async def test_passages_reject_invalid_public_parameters(params) -> None:
    service = BrokerService({"web": PassageCorpusBackend([])})
    service.register_session(make_session())

    with pytest.raises(ValueError):
        await service.call("token", "content.passages", params)


async def test_passages_empty_refs_and_exact_duplicates_are_successful() -> None:
    backend = PassageCorpusBackend(["alpha evidence", "beta evidence"])
    service = BrokerService({"web": backend})
    state = service.register_session(make_session())
    hits = await service.call("token", "search.query", {"query": "seed", "limit": 2})

    empty = await service.call(
        "token",
        "content.passages",
        {"query": "evidence", "refs": []},
    )
    report = await service.call(
        "token",
        "content.passages",
        {
            "query": "evidence",
            "refs": [hits[0]["ref"], hits[0]["ref"], hits[1]["ref"]],
            "limit": 2,
            "max_per_ref": 1,
        },
    )

    assert empty == {
        "query": "evidence",
        "passages": [],
        "failures": [],
        "input_count": 0,
        "unique_ref_count": 0,
    }
    assert report["input_count"] == 3
    assert report["unique_ref_count"] == 2
    assert [row["ref"] for row in report["passages"]] == [
        hits[0]["ref"],
        hits[1]["ref"],
    ]
    assert backend.fetched == [0, 1]
    assert state.policy.usage.content_fetches == 2
    assert state.policy.usage.intra_call_deduplicated_items == 1


async def test_passages_apply_ref_limit_before_deduplication() -> None:
    backend = PassageCorpusBackend(["alpha"])
    service = BrokerService({"web": backend}, max_content_refs_per_request=2)
    service.register_session(make_session())
    ref = (await service.call("token", "search.query", {"query": "seed"}))[0]["ref"]

    with pytest.raises(ValueError, match="maximum of 2"):
        await service.call(
            "token",
            "content.passages",
            {"query": "alpha", "refs": [ref, ref, ref]},
        )


async def test_passages_bm25_supports_english_and_chinese_queries() -> None:
    backend = PassageCorpusBackend(
        [
            "The audited report states that Singapore revenue reached 42 million dollars.",
            "公司公告显示，新加坡营收达到四千二百万美元。",
        ]
    )
    service = BrokerService({"web": backend})
    service.register_session(make_session())
    hits = await service.call("token", "search.query", {"query": "seed", "limit": 2})
    refs = [hit["ref"] for hit in hits]

    english = await service.call(
        "token",
        "content.passages",
        {"query": "Singapore revenue", "refs": refs, "limit": 1},
        execution_id="passages-lexical",
    )
    chinese = await service.call(
        "token",
        "content.passages",
        {"query": "新加坡 营收", "refs": refs, "limit": 1},
    )

    assert english["passages"][0]["ref"] == refs[0]
    assert chinese["passages"][0]["ref"] == refs[1]
    assert english["passages"][0]["ranker"] == "lexical:bm25"
    assert chinese["passages"][0]["score"] > 0
    trace = service.take_trace("token", "passages-lexical")[0]
    assert not any(
        attempt.operation == "web.rerank" for attempt in trace.provider_attempts
    )


async def test_passages_stably_break_ties_and_apply_max_per_ref_after_ranking() -> None:
    backend = PassageCorpusBackend(["a" * 180, "b" * 180])
    service = BrokerService(
        {"web": backend},
        passage_chunk_chars=50,
        passage_chunk_overlap_chars=10,
    )
    service.register_session(make_session())
    hits = await service.call("token", "search.query", {"query": "seed", "limit": 2})
    refs = [hit["ref"] for hit in hits]

    first = await service.call(
        "token",
        "content.passages",
        {"query": "unmatched", "refs": refs, "limit": 4, "max_per_ref": 2},
    )
    second = await service.call(
        "token",
        "content.passages",
        {"query": "unmatched", "refs": refs, "limit": 4, "max_per_ref": 2},
    )

    assert first["passages"] == second["passages"]
    assert [row["ref"] for row in first["passages"]] == [refs[0], refs[0], refs[1], refs[1]]
    assert [row["rank"] for row in first["passages"]] == [1, 2, 3, 4]
    assert [row["coordinates"]["start_character"] for row in first["passages"][:2]] == [
        0,
        40,
    ]


async def test_passages_keep_partial_fetch_failures_and_issue_resolvable_locators() -> None:
    backend = PassageCorpusBackend(
        ["alpha answer", "unavailable", "alpha corroboration"],
        fail={1},
    )
    service = BrokerService({"web": backend})
    service.register_session(make_session())
    hits = await service.call("token", "search.query", {"query": "seed", "limit": 3})
    refs = [hit["ref"] for hit in hits]

    report = await service.call(
        "token",
        "content.passages",
        {"query": "alpha", "refs": refs, "limit": 2, "max_per_ref": 1},
        execution_id="passages-partial",
    )

    assert report["failures"][0]["input_index"] == 1
    assert report["failures"][0]["ref"] == refs[1]
    assert report["failures"][0]["failure"]["code"] == "provider_rejected"
    assert [row["ref"] for row in report["passages"]] == [refs[0], refs[2]]
    assert all(row["locator"] is not None for row in report["passages"])
    resolved = await service.call(
        "token",
        "citations.resolve",
        {
            "requests": [
                {
                    "ref": report["passages"][0]["ref"],
                    "locator": report["passages"][0]["locator"],
                }
            ]
        },
    )
    assert resolved[0]["evidence"] == report["passages"][0]["text"]
    trace = service.take_trace("token", "passages-partial")[0]
    assert trace.result_count == 2
    assert len(trace.passage_records) == 2
    assert "alpha answer" not in trace.model_dump_json()
    assert trace.passage_records[0].coordinates == report["passages"][0]["coordinates"]


async def test_passages_only_register_final_rows_and_report_capacity_exhaustion() -> None:
    backend = PassageCorpusBackend(["alpha " * 100, "alpha " * 100])
    service = BrokerService(
        {"web": backend},
        passage_chunk_chars=80,
        passage_chunk_overlap_chars=10,
        max_evidence_records=1,
    )
    state = service.register_session(make_session())
    hits = await service.call("token", "search.query", {"query": "seed", "limit": 2})

    report = await service.call(
        "token",
        "content.passages",
        {
            "query": "alpha",
            "refs": [hit["ref"] for hit in hits],
            "limit": 2,
            "max_per_ref": 1,
        },
    )

    assert len(state.evidence) == 1
    assert report["passages"][0]["locator"] is not None
    assert report["passages"][1].get("locator") is None
    assert report["passages"][1]["locator_error"]["code"] == (
        "evidence_capacity_exhausted"
    )


async def test_passage_reranker_maps_scores_by_index_even_when_results_are_unordered() -> None:
    class UnorderedReranker:
        name = "test:unordered"
        provider_identity = "test:unordered"

        def preflight(self) -> None:
            return None

        async def rerank(self, query, documents):
            del query
            return [
                PassageRerankResult(index=1, score=9.0),
                PassageRerankResult(index=0, score=1.0),
            ][: len(documents)]

    backend = PassageCorpusBackend(["alpha first", "alpha second"])
    service = BrokerService({"web": backend}, passage_reranker=UnorderedReranker())
    service.register_session(make_session())
    hits = await service.call("token", "search.query", {"query": "seed", "limit": 2})

    report = await service.call(
        "token",
        "content.passages",
        {
            "query": "alpha",
            "refs": [hit["ref"] for hit in hits],
            "limit": 2,
            "max_per_ref": 1,
        },
        execution_id="passages-rerank",
    )

    assert [row["ref"] for row in report["passages"]] == [hits[1]["ref"], hits[0]["ref"]]
    assert [row["score"] for row in report["passages"]] == [9.0, 1.0]
    trace = service.take_trace("token", "passages-rerank")[0]
    rerank_attempts = [
        attempt for attempt in trace.provider_attempts if attempt.operation == "web.rerank"
    ]
    assert rerank_attempts[0].request_indexes == [0, 1]


async def test_passage_prefilter_keeps_eight_per_ref_then_caps_globally_at_100() -> None:
    class CapturingReranker:
        name = "test:capture"
        provider_identity = "test:capture"

        def __init__(self) -> None:
            self.documents: list[str] = []

        def preflight(self) -> None:
            return None

        async def rerank(self, query, documents):
            del query
            self.documents = list(documents)
            return [
                PassageRerankResult(index=index, score=0.0)
                for index in range(len(documents))
            ]

    reranker = CapturingReranker()
    backend = PassageCorpusBackend([chr(97 + index) * 200 for index in range(13)])
    service = BrokerService(
        {"web": backend},
        passage_reranker=reranker,
        passage_chunk_chars=20,
        passage_chunk_overlap_chars=0,
    )
    service.register_session(make_session())
    hits = await service.call("token", "search.query", {"query": "seed", "limit": 13})

    await service.call(
        "token",
        "content.passages",
        {
            "query": "unmatched",
            "refs": [hit["ref"] for hit in hits],
            "limit": 1,
            "max_per_ref": 3,
        },
    )

    counts = Counter(document[0] for document in reranker.documents)
    assert len(reranker.documents) == 100
    assert [counts[chr(97 + index)] for index in range(13)] == [*([8] * 12), 4]


async def test_jina_mode_has_no_silent_lexical_fallback_but_empty_pages_succeed() -> None:
    empty = BrokerService(
        {"web": PassageCorpusBackend([""])},
        passage_reranker=JinaPassageReranker(),
    )
    empty.register_session(make_session())
    empty_ref = (await empty.call("token", "search.query", {"query": "seed"}))[0]["ref"]
    report = await empty.call(
        "token",
        "content.passages",
        {"query": "answer", "refs": [empty_ref]},
    )
    assert report["passages"] == []

    configured = BrokerService(
        {"web": PassageCorpusBackend(["lexically matching answer"])},
        passage_reranker=JinaPassageReranker(),
    )
    state = configured.register_session(make_session())
    ref = (await configured.call("token", "search.query", {"query": "seed"}))[0]["ref"]
    with pytest.raises(ProviderRequestError) as raised:
        await configured.call(
            "token",
            "content.passages",
            {"query": "answer", "refs": [ref]},
            execution_id="passages-missing-jina",
        )

    assert raised.value.code == "provider_not_configured"
    assert raised.value.attempts == 0
    assert state.evidence == {}
    trace = configured.take_trace("token", "passages-missing-jina")[0]
    assert trace.status == "error"
    assert not any(
        attempt.operation == "web.rerank" for attempt in trace.provider_attempts
    )


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


async def test_extract_many_returns_checked_rows_and_rejects_non_strict_json() -> None:
    service, _ = make_scripted_llm_service(
        [
            '{"name":"valid"}',
            "[]",
            '{"name":"first","name":"duplicate"}',
            '{"name":NaN}',
            '{"name":1e400}',
            "   ",
        ]
    )
    schema = {
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
        "additionalProperties": False,
    }

    rows = await service.call(
        "token",
        "llm.extract_many",
        {
            "items": list(range(6)),
            "instruction": "Extract a name",
            "schema": schema,
            "concurrency": 1,
        },
        execution_id="exec-extract",
    )

    assert rows[0] == {
        "index": 0,
        "data": {"name": "valid"},
        "error": None,
        "attempts": 1,
    }
    assert [row["error"]["code"] for row in rows[1:]] == [
        "non_object",
        "invalid_json",
        "invalid_json",
        "invalid_json",
        "empty_output",
    ]
    assert all(row["data"] is None for row in rows[1:])
    event = service.take_trace("token", "exec-extract")[0]
    assert event.model_tokens == 42
    assert [attempt.index for attempt in event.model_attempts] == list(range(6))
    assert [attempt.error_code for attempt in event.model_attempts] == [
        None,
        "non_object",
        "invalid_json",
        "invalid_json",
        "invalid_json",
        "empty_output",
    ]
    assert event.result_payload is None
    assert set(event.model_attempts[0].model_dump()) == {
        "index",
        "phase",
        "status",
        "duration_seconds",
        "model_tokens",
        "error_code",
    }


async def test_extract_many_supports_nested_arrays_nullable_scalars_and_enum() -> None:
    service, _ = make_scripted_llm_service(
        ['{"status":"ok","note":null,"tags":["a"],"details":{"count":2}}']
    )
    rows = await service.call(
        "token",
        "llm.extract_many",
        {
            "items": ["input"],
            "instruction": "extract",
            "schema": {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "properties": {
                    "status": {"enum": ["ok", "failed"]},
                    "note": {"type": ["string", "null"]},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "details": {
                        "type": "object",
                        "properties": {"count": {"type": "integer"}},
                        "required": ["count"],
                        "additionalProperties": False,
                    },
                },
                "required": ["status", "note", "tags", "details"],
                "additionalProperties": False,
            },
        },
    )

    assert rows[0]["error"] is None
    assert rows[0]["data"]["details"] == {"count": 2}


async def test_extract_many_validates_schema_and_limits_before_charging() -> None:
    service, client = make_scripted_llm_service(
        ['{"ok":true}'],
        max_extract_instruction_bytes=3,
        max_extract_item_bytes=4,
    )
    state = service.sessions["token"]

    with pytest.raises(ValueError, match="JSON-serializable"):
        await service.call(
            "token",
            "llm.extract_many",
            {
                "items": [1],
                "instruction": "",
                "schema": {
                    "type": "object",
                    "properties": {"ok": {"type": bool}},
                },
            },
        )
    with pytest.raises(ValueError, match="keyword 'pattern'.*not supported"):
        await service.call(
            "token",
            "llm.extract_many",
            {
                "items": [1],
                "instruction": "",
                "schema": {
                    "type": "object",
                    "properties": {"ok": {"type": "string", "pattern": "x"}},
                },
            },
        )
    with pytest.raises(ValueError, match="one type plus null"):
        await service.call(
            "token",
            "llm.extract_many",
            {
                "items": [1],
                "instruction": "",
                "schema": {
                    "type": "object",
                    "properties": {"ok": {"type": [{}, "null"]}},
                },
            },
        )
    with pytest.raises(ValueError, match="instruction is 4 bytes"):
        await service.call(
            "token",
            "llm.extract_many",
            {
                "items": [1],
                "instruction": "four",
                "schema": {"type": "object"},
            },
        )
    with pytest.raises(ValueError, match="item at index 0 is 6 bytes"):
        await service.call(
            "token",
            "llm.extract_many",
            {
                "items": ["long"],
                "instruction": "",
                "schema": {"type": "object"},
            },
        )
    with pytest.raises(ValueError, match="concurrency must be an integer"):
        await service.call(
            "token",
            "llm.extract_many",
            {
                "items": [1],
                "instruction": "",
                "schema": {"type": "object"},
                "concurrency": None,
            },
        )

    assert client.calls == []
    assert state.policy.usage.llm_calls == 0


async def test_extract_many_enforces_batch_schema_total_and_depth_limits() -> None:
    cases = [
        (
            {"max_extract_items": 1},
            {
                "items": [1, 2],
                "instruction": "",
                "schema": {"type": "object"},
            },
            "exceeding the broker maximum of 1",
        ),
        (
            {"max_extract_schema_bytes": 16},
            {
                "items": [1],
                "instruction": "",
                "schema": {"type": "object"},
            },
            "schema is 17 bytes",
        ),
        (
            {"max_extract_total_item_bytes": 3},
            {
                "items": [10, 20],
                "instruction": "",
                "schema": {"type": "object"},
            },
            "items total 4 bytes",
        ),
        (
            {"max_extract_schema_depth": 2},
            {
                "items": [1],
                "instruction": "",
                "schema": {
                    "type": "object",
                    "properties": {
                        "outer": {
                            "type": "object",
                            "properties": {"inner": {"type": "string"}},
                        }
                    },
                },
            },
            "schema nesting exceeds maximum depth 2",
        ),
    ]

    for limits, params, message in cases:
        service, client = make_scripted_llm_service(['{"ok":true}'], **limits)
        state = service.sessions["token"]
        with pytest.raises(ValueError, match=message):
            await service.call("token", "llm.extract_many", params)
        assert client.calls == []
        assert state.policy.usage.llm_calls == 0


async def test_extract_many_keeps_partial_provider_failure_and_success_tokens() -> None:
    service, _ = make_scripted_llm_service(
        [RuntimeError("provider secret response body"), '{"value":2}']
    )
    rows = await service.call(
        "token",
        "llm.extract_many",
        {
            "items": [1, 2],
            "instruction": "extract",
            "schema": {
                "type": "object",
                "properties": {"value": {"type": "integer"}},
                "required": ["value"],
            },
            "concurrency": 1,
        },
        execution_id="exec-partial",
    )

    assert rows[0]["error"] == {
        "code": "provider_error",
        "message": "Extraction provider request failed",
        "retryable": True,
    }
    assert rows[1]["data"] == {"value": 2}
    assert service.sessions["token"].policy.usage.pipeline_model_tokens == 7
    event = service.take_trace("token", "exec-partial")[0]
    assert event.model_tokens == 7
    assert "secret" not in event.model_dump_json()


async def test_extract_many_raises_safe_typed_error_when_provider_fails_all_items() -> None:
    service, _ = make_scripted_llm_service(
        [RuntimeError("first secret"), RuntimeError("second secret")]
    )
    with pytest.raises(ExtractionInfrastructureError) as raised:
        await service.call(
            "token",
            "llm.extract_many",
            {
                "items": [1, 2],
                "instruction": "extract",
                "schema": {"type": "object"},
                "concurrency": 1,
            },
            execution_id="exec-provider-down",
        )

    assert raised.value.code == "extraction_provider_unavailable"
    assert raised.value.retryable is True
    assert "secret" not in str(raised.value)
    event = service.take_trace("token", "exec-provider-down")[0]
    assert [attempt.error_code for attempt in event.model_attempts] == [
        "provider_error",
        "provider_error",
    ]
    assert "secret" not in event.model_dump_json()


async def test_extract_many_repairs_in_index_order_after_one_budget_reservation() -> None:
    service, client = make_scripted_llm_service(
        ["not json", '{"value":"wrong"}', '{"value":1}', '{"value":"still wrong"}']
    )
    rows = await service.call(
        "token",
        "llm.extract_many",
        {
            "items": [1, 2],
            "instruction": "extract",
            "schema": {
                "type": "object",
                "properties": {"value": {"type": "integer"}},
                "required": ["value"],
            },
            "repair_attempts": 1,
            "concurrency": 1,
        },
        execution_id="exec-repair",
    )

    assert rows[0] == {
        "index": 0,
        "data": {"value": 1},
        "error": None,
        "attempts": 2,
    }
    assert rows[1]["attempts"] == 2
    assert rows[1]["error"]["code"] == "schema_mismatch"
    state = service.sessions["token"]
    assert state.policy.usage.llm_calls == 4
    assert state.policy.usage.pipeline_model_tokens == 28
    event = service.take_trace("token", "exec-repair")[0]
    assert [(attempt.index, attempt.phase) for attempt in event.model_attempts] == [
        (0, "initial"),
        (1, "initial"),
        (0, "repair"),
        (1, "repair"),
    ]
    repair_prompts = [call["messages"][-1]["content"] for call in client.calls[2:]]
    assert "Validation error:\ninvalid_json:" in repair_prompts[0]
    assert "Validation error:\nschema_mismatch:" in repair_prompts[1]
    assert all("provider_error" not in prompt for prompt in repair_prompts)


async def test_extract_many_reserves_all_repairs_before_dispatch() -> None:
    service, client = make_scripted_llm_service(
        ["not json", '{"value":2}', '{"value":1}'],
        budget=ResourceBudget(max_pipeline_llm_calls=2),
    )
    with pytest.raises(BudgetExceeded, match="max_pipeline_llm_calls"):
        await service.call(
            "token",
            "llm.extract_many",
            {
                "items": [1, 2],
                "instruction": "extract",
                "schema": {"type": "object"},
                "repair_attempts": 1,
                "concurrency": 1,
            },
        )

    assert len(client.calls) == 2
    assert service.sessions["token"].policy.usage.pipeline_model_tokens == 14


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


async def test_grep_report_line_numbers_are_read_offsets() -> None:
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

    report = await service.call(
        "token",
        "content.grep_report",
        {"refs": [ref], "pattern": r"target \w+", "context": 1},
    )
    matches = report["matches"]
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


async def test_grep_report_falls_back_to_a_literal_search_for_a_bad_pattern() -> None:
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

    report = await service.call(
        "token", "content.grep_report", {"refs": [ref], "pattern": "C++ ("}
    )
    matches = report["matches"]
    assert [match["line"] for match in matches] == [1]


async def test_selected_passage_locator_resolves_exact_evidence_and_rejects_tampering() -> None:
    class EvidenceBackend(FakeBackend):
        async def content(self, hits, *, query=None):
            return [
                ContentSnippet(
                    ref=hit.ref,
                    text=f"document {hit.rank} line one\ndocument {hit.rank} line two",
                    url=hit.url,
                )
                for hit in hits
            ]

    service = BrokerService({"web": EvidenceBackend("web", depth=2)})
    service.register_session(make_session())
    hits = await service.call("token", "search.query", {"query": "q", "limit": 2})
    rows = await service.call(
        "token",
        "content.read",
        {"refs": [hits[0]["ref"]], "offset": 2, "limit": 1},
        execution_id="exec-evidence-issue",
    )
    locator = rows[0]["locator"]
    issued = service.take_trace("token", "exec-evidence-issue")[0]
    assert len(issued.evidence_records) == 1
    assert issued.evidence_records[0].action == "issue"
    assert issued.evidence_records[0].status == "ok"
    assert issued.evidence_records[0].coordinates == {
        "type": "lines",
        "start_line": 2,
        "end_line": 2,
    }
    assert len(issued.evidence_records[0].document_fingerprint or "") == 64
    assert "document 1 line two" not in issued.model_dump_json()

    selected = await service.call(
        "token",
        "citations.resolve",
        {"requests": [{"ref": hits[0]["ref"], "locator": locator}]},
        execution_id="exec-evidence-validate",
    )
    assert selected[0]["evidence"] == rows[0]["text"] == "document 1 line two"
    assert selected[0]["evidence_kind"] == "selected_passage"
    validated = service.take_trace("token", "exec-evidence-validate")[0]
    assert validated.evidence_records[0].action == "validate"
    assert validated.evidence_records[0].status == "ok"
    # Default context decoupling keeps the citation body out of the trace. The
    # explicit context-decoupling ablation remains the one intentional echo arm.
    assert validated.result_payload is None
    assert "document 1 line two" not in validated.model_dump_json()
    legacy = await service.call(
        "token", "citations.resolve", {"refs": [hits[0]["ref"]]}
    )
    assert legacy[0]["evidence_kind"] == "search_preview"
    assert legacy[0]["evidence"] == "snippet"

    with pytest.raises(ValueError, match="Unknown evidence locator"):
        await service.call(
            "token",
            "citations.resolve",
            {
                "requests": [
                    {
                        "ref": hits[0]["ref"],
                        "locator": {**locator, "id": "evidence_unknown"},
                    }
                ]
            },
            execution_id="exec-evidence-invalid",
        )
    invalid = service.take_trace("token", "exec-evidence-invalid")[0]
    assert invalid.evidence_records[0].status == "error"
    assert invalid.evidence_records[0].error_code == "unknown_locator"
    with pytest.raises(ValueError, match="does not belong"):
        await service.call(
            "token",
            "citations.resolve",
            {"requests": [{"ref": hits[1]["ref"], "locator": locator}]},
        )
    with pytest.raises(ValueError, match="exactly id, ref, and kind"):
        await service.call(
            "token",
            "citations.resolve",
            {
                "requests": [
                    {
                        "ref": hits[0]["ref"],
                        "locator": {**locator, "extra": "tampered"},
                    }
                ]
            },
        )
    with pytest.raises(ValueError, match="too long"):
        await service.call(
            "token",
            "citations.resolve",
            {
                "requests": [
                    {
                        "ref": hits[0]["ref"],
                        "locator": {**locator, "id": "x" * 129},
                    }
                ]
            },
        )


async def test_read_uses_character_locator_when_one_line_is_truncated() -> None:
    class LongLineBackend(FakeBackend):
        async def content(self, hits, *, query=None):
            return [ContentSnippet(ref=hit.ref, text="x" * 30 + "\nnext") for hit in hits]

    service = BrokerService({"web": LongLineBackend("web")})
    service.register_session(make_session())
    ref = (await service.call("token", "search.query", {"query": "q"}))[0]["ref"]

    rows = await service.call(
        "token",
        "content.read",
        {"refs": [ref], "limit": 1, "max_chars": 10},
        execution_id="exec-partial-line",
    )

    assert rows[0]["text"] == "x" * 10
    assert rows[0]["metadata"]["truncated_by_max_chars"] is True
    assert rows[0]["metadata"]["truncated_mid_line"] is True
    assert rows[0]["metadata"]["partial_line_remaining_chars"] == 20
    evidence = service.take_trace("token", "exec-partial-line")[0].evidence_records[0]
    assert evidence.coordinates == {
        "type": "line_characters",
        "line": 1,
        "start_character": 0,
        "end_character": 10,
    }
    resolved = await service.call(
        "token",
        "citations.resolve",
        {"requests": [{"ref": ref, "locator": rows[0]["locator"]}]},
    )
    assert resolved[0]["evidence"] == "x" * 10


async def test_grep_report_locator_cites_context_and_honours_size_limit() -> None:
    class EvidenceBackend(FakeBackend):
        async def content(self, hits, *, query=None):
            return [ContentSnippet(ref=hit.ref, text="before\ntarget\nafter") for hit in hits]

    service = BrokerService({"web": EvidenceBackend("web")}, max_evidence_chars=19)
    service.register_session(make_session())
    ref = (await service.call("token", "search.query", {"query": "q"}))[0]["ref"]
    report = await service.call(
        "token",
        "content.grep_report",
        {"refs": [ref], "pattern": "target", "context": 1},
    )
    matches = report["matches"]
    resolved = await service.call(
        "token",
        "citations.resolve",
        {"requests": [{"ref": ref, "locator": matches[0]["locator"]}]},
    )
    assert resolved[0]["evidence"] == "before\ntarget\nafter"

    too_small = BrokerService({"web": EvidenceBackend("web")}, max_evidence_chars=5)
    too_small.register_session(make_session())
    small_ref = (
        await too_small.call("token", "search.query", {"query": "q"})
    )[0]["ref"]
    small_report = await too_small.call(
        "token", "content.grep_report", {"refs": [small_ref], "pattern": "target"}
    )
    small_matches = small_report["matches"]
    assert small_matches[0].get("locator") is None


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
    """grep_report and read are meant to be used repeatedly over one pool.

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
    await service.call("token", "content.grep_report", {"refs": refs, "pattern": "body"})
    await service.call("token", "content.read", {"refs": refs})

    assert backend.fetched == ["1", "2", "3"]
    # Both numbers are reported: one follows the program's behaviour, the other
    # follows the bill, and a cache is exactly what makes them diverge.
    assert state.policy.usage.content_fetches == 9
    assert state.policy.usage.content_backend_fetches == 3


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
    assert rows[1]["failure"]["message"] == "Provider rejected one document."
    assert "HTTPError" not in json.dumps(rows[1])
    assert rows[1]["text"] == ""
    # A failure is not cached: a transient timeout must not be frozen for the
    # rest of the rollout.
    await service.call("token", "content.get_many", {"refs": refs})
    assert backend.fetched == ["1", "2", "3", "2"]


async def test_all_permanent_document_failures_remain_typed_aligned_rows() -> None:
    backend = CountingBackend(fail={"1", "2"})
    service = BrokerService({"local": backend})
    service.register_session(make_session(backends=["local"]))
    hits = await service.call("token", "search.query", {"query": "q", "limit": 2})

    rows = await service.call(
        "token",
        "content.get_many",
        {"refs": [hit["ref"] for hit in hits]},
        execution_id="sanitized-content",
    )

    assert [row["failure"]["code"] for row in rows] == [
        "provider_rejected",
        "provider_rejected",
    ]
    assert all(row["text"] == "" and row.get("locator") is None for row in rows)
    assert [row["failure"]["message"] for row in rows] == [
        "Provider rejected one document.",
        "Provider rejected one document.",
    ]
    event = service.take_trace("token", "sanitized-content")[0]
    assert "HTTPError" not in event.model_dump_json()


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
    assert set(usage) == {
        "exec_calls",
        "search_calls",
        "content_fetches",
        "llm_calls",
        "pipeline_model_tokens",
        "documents_seen",
        "budget_remaining",
        "terminal_reason",
    }


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
            "content.grep_report",
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
    for removed in ("content.snippets", "content.grep"):
        with pytest.raises(ValueError, match="Unsupported capability"):
            await service.call("token", removed, {})


def test_capabilities_manifest_drops_only_what_is_disabled() -> None:
    assert CAPABILITY_METHODS == BROKER_METHODS
    assert Mechanisms().capabilities() == list(CAPABILITY_METHODS)
    assert "content.snippets" not in CAPABILITY_METHODS
    assert "content.grep" not in CAPABILITY_METHODS
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


async def _wait_for_condition(predicate, *, turns: int = 200) -> None:
    for _ in range(turns):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition did not become true")


class _CoalescingBatchBackend:
    name = "local"
    provider_identity = "test:local:coalescing"
    supports_domains = False
    max_depth = None

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.release = asyncio.Event()

    @staticmethod
    def _batch(query: str) -> SearchBatch:
        return SearchBatch(
            query=query,
            hits=[
                SearchHit(
                    ref="",
                    backend="local",
                    title=query,
                    docid=f"doc-{query}",
                    snippet="snippet",
                    rank=1,
                )
            ],
        )

    async def search(self, query, *, limit, offset=0, domains=None):
        self.calls.append([query])
        await self.release.wait()
        return list(self._batch(query).hits)

    async def search_many(self, queries, *, limit, offset=0, domains=None):
        self.calls.append(list(queries))
        await self.release.wait()
        return [self._batch(query) for query in queries]

    async def fetch(self, hit, *, query=None):
        return ContentSnippet(ref=hit.ref, text=f"body:{hit.docid}")


class _CoalescingSearchBackend:
    name = "web"
    provider_identity = "test:web:coalescing"
    supports_domains = True
    max_depth = None

    def __init__(self) -> None:
        self.calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = False

    async def search(self, query, *, limit, offset=0, domains=None):
        self.calls += 1
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return [
            SearchHit(
                ref="",
                backend="web",
                title=query,
                url=f"https://example.com/{query}",
                snippet="snippet",
                rank=1,
                metadata={"nested": {"value": query}},
            )
        ]

    async def fetch(self, hit, *, query=None):
        return ContentSnippet(ref=hit.ref, text="body", url=hit.url)


class _CoalescingContentBackend(FakeBackend):
    provider_identity = "test:web:content-coalescing"

    def __init__(self) -> None:
        super().__init__("web")
        self.fetch_calls = 0
        self.fetch_started = asyncio.Event()
        self.fetch_release = asyncio.Event()

    async def fetch(self, hit, *, query=None):
        self.fetch_calls += 1
        self.fetch_started.set()
        await self.fetch_release.wait()
        return ContentSnippet(
            ref=hit.ref,
            text="shared full document",
            url=hit.url,
            metadata={"nested": {"value": 1}},
        )


async def test_inflight_coalescing_overlapping_local_batches_and_duplicates() -> None:
    backend = _CoalescingBatchBackend()
    service = BrokerService({"local": backend}, inflight_coalescing=True)
    state = service.register_session(make_session(backends=["local"]))

    first = asyncio.create_task(
        service.call(
            "token",
            "search.query_many",
            {"queries": ["alpha", "beta"]},
            execution_id="first",
        )
    )
    await _wait_for_condition(lambda: len(backend.calls) == 1)
    second = asyncio.create_task(
        service.call(
            "token",
            "search.query_many",
            {"queries": ["beta", "beta", "gamma"]},
            execution_id="second",
        )
    )
    await _wait_for_condition(lambda: len(backend.calls) == 2)

    assert backend.calls == [["alpha", "beta"], ["gamma"]]
    assert sorted(entry.waiters for entry in state.flights.values()) == [1, 1, 2]
    backend.release.set()
    first_rows, second_rows = await asyncio.gather(first, second)

    assert [row["query"] for row in first_rows] == ["alpha", "beta"]
    assert [row["query"] for row in second_rows] == ["beta", "beta", "gamma"]
    assert second_rows[0] is not second_rows[1]
    second_rows[0]["hits"][0]["title"] = "changed"
    assert second_rows[1]["hits"][0]["title"] == "beta"
    assert state.flights == {}
    assert state.policy.usage.search_provider_attempts == 2
    assert state.policy.usage.provider_coalesced_requests == 1
    assert state.policy.usage.intra_call_deduplicated_items == 1

    first_trace = service.take_trace("token", "first")
    second_trace = service.take_trace("token", "second")
    assert len(first_trace[0].provider_attempts) == 1
    assert len(second_trace[0].provider_attempts) == 1
    assert len(second_trace[0].coalesced_requests) == 1
    assert len(second_trace[0].deduplicated_requests) == 1


async def test_inflight_admission_is_atomic_before_provider_side_effect() -> None:
    backend = _CoalescingBatchBackend()
    service = BrokerService(
        {"local": backend},
        inflight_coalescing=True,
        max_inflight_keys=1,
    )
    state = service.register_session(make_session(backends=["local"]))
    leader = asyncio.create_task(
        service.call("token", "search.query", {"query": "alpha"})
    )
    await _wait_for_condition(lambda: len(backend.calls) == 1)

    with pytest.raises(InflightCapacityError):
        await service.call(
            "token",
            "search.query_many",
            {"queries": ["alpha", "beta"]},
        )

    assert backend.calls == [["alpha"]]
    assert len(state.flights) == 1
    assert next(iter(state.flights.values())).waiters == 1
    backend.release.set()
    await leader


async def test_inflight_waiter_limit_counts_unique_call_not_duplicate_rows() -> None:
    backend = _CoalescingBatchBackend()
    service = BrokerService(
        {"local": backend},
        inflight_coalescing=True,
        max_waiters_per_flight=2,
    )
    state = service.register_session(make_session(backends=["local"]))
    leader = asyncio.create_task(
        service.call("token", "search.query", {"query": "same"})
    )
    await _wait_for_condition(lambda: len(backend.calls) == 1)
    duplicate_follower = asyncio.create_task(
        service.call(
            "token",
            "search.query_many",
            {"queries": ["same", "same"]},
        )
    )
    await _wait_for_condition(
        lambda: next(iter(state.flights.values())).waiters == 2
    )

    with pytest.raises(InflightCapacityError):
        await service.call("token", "search.query", {"query": "same"})
    assert backend.calls == [["same"]]
    assert next(iter(state.flights.values())).waiters == 2

    backend.release.set()
    leader_rows, follower_rows = await asyncio.gather(leader, duplicate_follower)
    assert leader_rows[0]["title"] == "same"
    assert len(follower_rows) == 2


async def test_inflight_feature_disabled_keeps_independent_transports() -> None:
    backend = _CoalescingSearchBackend()
    service = BrokerService({"web": backend}, inflight_coalescing=False)
    state = service.register_session(make_session())

    calls = [
        asyncio.create_task(service.call("token", "search.query", {"query": "same"}))
        for _ in range(2)
    ]
    await _wait_for_condition(lambda: backend.calls == 2)
    assert state.flights == {}
    backend.release.set()
    await asyncio.gather(*calls)
    assert state.policy.usage.provider_coalesced_requests == 0
    assert state.policy.usage.search_provider_attempts == 2


async def test_cancelling_leader_detaches_without_cancelling_follower() -> None:
    backend = _CoalescingSearchBackend()
    service = BrokerService({"web": backend}, inflight_coalescing=True)
    state = service.register_session(make_session())
    leader = asyncio.create_task(
        service.call(
            "token",
            "search.query",
            {"query": "same"},
            execution_id="leader",
        )
    )
    await backend.started.wait()
    follower = asyncio.create_task(
        service.call(
            "token",
            "search.query",
            {"query": "same"},
            execution_id="follower",
        )
    )
    await _wait_for_condition(
        lambda: len(state.flights) == 1
        and next(iter(state.flights.values())).waiters == 2
    )

    await service.cancel_execution("token", "leader")
    with pytest.raises(asyncio.CancelledError):
        await leader
    assert not backend.cancelled
    assert next(iter(state.flights.values())).waiters == 1

    backend.release.set()
    result = await follower
    assert result[0]["title"] == "same"
    assert state.flights == {}
    leader_trace = service.take_trace("token", "leader")
    follower_trace = service.take_trace("token", "follower")
    assert leader_trace[0].status == "cancelled"
    assert len(leader_trace[0].provider_attempts) == 1
    assert follower_trace[0].status == "ok"
    assert follower_trace[0].provider_attempts == []
    assert len(follower_trace[0].coalesced_requests) == 1
    assert state.policy.usage.search_provider_attempts == 1


async def test_cancel_execution_drains_last_waiter_group_and_trace() -> None:
    backend = _CoalescingSearchBackend()
    service = BrokerService({"web": backend}, inflight_coalescing=True)
    state = service.register_session(make_session())
    call = asyncio.create_task(
        service.call(
            "token",
            "search.query",
            {"query": "cancel"},
            execution_id="cancel-last",
        )
    )
    await backend.started.wait()

    await service.cancel_execution("token", "cancel-last")

    with pytest.raises(asyncio.CancelledError):
        await call
    assert backend.cancelled
    assert state.flights == {}
    trace = service.take_trace("token", "cancel-last")
    assert len(trace) == 1
    assert trace[0].status == "cancelled"
    assert [attempt.status for attempt in trace[0].provider_attempts] == ["cancelled"]
    await asyncio.sleep(0)
    assert service.take_trace("token", "cancel-last") == []


async def test_last_waiter_cancel_cleans_flight_during_publish_lock_race() -> None:
    backend = _CoalescingSearchBackend()
    service = BrokerService({"web": backend}, inflight_coalescing=True)
    state = service.register_session(make_session())
    call = asyncio.create_task(
        service.call("token", "search.query", {"query": "race"})
    )
    await backend.started.wait()
    entry = next(iter(state.flights.values()))

    await state.flight_lock.acquire()
    call.cancel()
    await asyncio.sleep(0)
    backend.release.set()
    await asyncio.sleep(0)
    state.flight_lock.release()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(call, timeout=1)
    assert state.flights == {}
    assert entry.future.done()
    retry = await asyncio.wait_for(
        service.call("token", "search.query", {"query": "race"}),
        timeout=1,
    )
    assert retry[0]["title"] == "race"
    assert backend.calls == 2


async def test_content_coalescing_counts_only_real_backend_leader_and_copies_rows() -> None:
    backend = _CoalescingContentBackend()
    service = BrokerService({"web": backend}, inflight_coalescing=True)
    state = service.register_session(make_session())
    hits = await service.call("token", "search.query", {"query": "doc"})
    ref = hits[0]["ref"]

    first = asyncio.create_task(
        service.call(
            "token",
            "content.get_many",
            {"refs": [ref]},
            execution_id="content-leader",
        )
    )
    await backend.fetch_started.wait()
    second = asyncio.create_task(
        service.call(
            "token",
            "content.get_many",
            {"refs": [ref]},
            execution_id="content-follower",
        )
    )
    await _wait_for_condition(
        lambda: len(state.flights) == 1
        and next(iter(state.flights.values())).waiters == 2
    )
    backend.fetch_release.set()
    first_rows, second_rows = await asyncio.gather(first, second)

    assert backend.fetch_calls == 1
    assert state.policy.usage.content_fetches == 2
    assert state.policy.usage.content_backend_fetches == 1
    assert state.policy.usage.provider_coalesced_requests == 1
    first_rows[0]["metadata"]["nested"]["value"] = 9
    assert second_rows[0]["metadata"]["nested"]["value"] == 1
    leader_trace = service.take_trace("token", "content-leader")
    follower_trace = service.take_trace("token", "content-follower")
    assert len(leader_trace[0].provider_attempts) == 1
    assert follower_trace[0].provider_attempts == []
    assert len(follower_trace[0].coalesced_requests) == 1


async def test_content_leader_caches_before_flight_cleanup_lock_queue() -> None:
    backend = _CoalescingContentBackend()
    service = BrokerService({"web": backend}, inflight_coalescing=True)
    state = service.register_session(make_session())
    ref = (await service.call("token", "search.query", {"query": "doc"}))[0][
        "ref"
    ]
    leader = asyncio.create_task(
        service.call("token", "content.get_many", {"refs": [ref]})
    )
    await backend.fetch_started.wait()

    # Hold the registry lock so the completed transport's cleanup is queued.
    # The cache must already be visible while the old flight is still present;
    # otherwise a third caller also queues on this lock and later starts a
    # duplicate fetch in the flight/cache publication gap.
    await state.flight_lock.acquire()
    try:
        backend.fetch_release.set()
        await _wait_for_condition(lambda: ref in state.content_cache)
        assert len(state.flights) == 1

        third = asyncio.create_task(
            service.call("token", "content.get_many", {"refs": [ref]})
        )
        await _wait_for_condition(third.done)
        assert (await third)[0]["text"] == "shared full document"
        assert backend.fetch_calls == 1
    finally:
        state.flight_lock.release()

    assert (await leader)[0]["text"] == "shared full document"
    assert state.flights == {}


async def test_content_refreshes_stale_misses_after_usage_reservation() -> None:
    backend = _CoalescingContentBackend()
    service = BrokerService({"web": backend}, inflight_coalescing=True)
    state = service.register_session(make_session())
    ref = (await service.call("token", "search.query", {"query": "doc"}))[0][
        "ref"
    ]
    original_record = state.policy.record_content_fetches
    follower_reserved = asyncio.Event()
    resume_follower = asyncio.Event()
    reservations = 0

    async def gated_record(requested: int, from_backend: int) -> None:
        nonlocal reservations
        reservations += 1
        await original_record(requested, from_backend)
        if reservations == 2:
            follower_reserved.set()
            await resume_follower.wait()

    state.policy.record_content_fetches = gated_record
    leader = asyncio.create_task(
        service.call("token", "content.get_many", {"refs": [ref]})
    )
    await backend.fetch_started.wait()
    follower = asyncio.create_task(
        service.call("token", "content.get_many", {"refs": [ref]})
    )
    await follower_reserved.wait()

    backend.fetch_release.set()
    assert (await leader)[0]["text"] == "shared full document"
    resume_follower.set()
    assert (await follower)[0]["text"] == "shared full document"
    assert backend.fetch_calls == 1
    assert state.policy.usage.content_fetches == 2
    assert state.policy.usage.content_backend_fetches == 1


async def test_content_rechecks_cache_after_waiting_for_flight_admission() -> None:
    backend = _CoalescingContentBackend()
    service = BrokerService({"web": backend}, inflight_coalescing=True)
    state = service.register_session(make_session())
    ref = (await service.call("token", "search.query", {"query": "doc"}))[0][
        "ref"
    ]
    leader = asyncio.create_task(
        service.call("token", "content.get_many", {"refs": [ref]})
    )
    await backend.fetch_started.wait()

    original_admit = service._admit_flights
    follower_at_admission = asyncio.Event()
    resume_follower = asyncio.Event()

    async def gated_admit(state_arg, requests, *, group_new):
        follower_at_admission.set()
        await resume_follower.wait()
        return await original_admit(state_arg, requests, group_new=group_new)

    service._admit_flights = gated_admit
    follower = asyncio.create_task(
        service.call("token", "content.get_many", {"refs": [ref]})
    )
    await follower_at_admission.wait()

    # The follower already classified the row as a miss, but has not acquired
    # the flight registry lock. Let the old leader cache and remove its flight
    # before the follower admits a new key.
    backend.fetch_release.set()
    assert (await leader)[0]["text"] == "shared full document"
    assert state.flights == {}
    resume_follower.set()

    assert (await follower)[0]["text"] == "shared full document"
    assert backend.fetch_calls == 1
    assert state.policy.usage.content_backend_fetches == 1


async def test_local_partial_batch_attempt_is_sanitized_and_traced_as_partial() -> None:
    class PartialBackend(_CoalescingBatchBackend):
        async def search_many(self, queries, *, limit, offset=0, domains=None):
            return [
                self._batch(queries[0]),
                SearchBatch(
                    query=queries[1],
                    failure={
                        "code": "provider_rejected",
                        "message": "secret provider response body",
                        "retryable": False,
                        "attempts": 1,
                    },
                ),
            ]

    backend = PartialBackend()
    service = BrokerService({"local": backend})
    service.register_session(make_session(backends=["local"]))

    rows = await service.call(
        "token",
        "search.query_many",
        {"queries": ["ok", "bad"]},
        execution_id="partial-batch",
    )

    assert len(rows[0]["hits"]) == 1
    assert rows[1]["failure"]["message"] == "Provider rejected one search item."
    assert "secret" not in json.dumps(rows)
    trace = service.take_trace("token", "partial-batch")[0]
    assert trace.provider_attempts[0].status == "partial"
    assert "secret" not in trace.model_dump_json()
