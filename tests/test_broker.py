from __future__ import annotations

import asyncio
import json
from collections import Counter
from contextlib import suppress
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from opensac_sdk._surface import BROKER_METHODS
from pydantic import ValidationError

from opensac.backends.document import DocumentContent, DocumentHandle
from opensac.backends.llm import OpenAICompatibleBackend
from opensac.backends.rerank.base import RerankScore
from opensac.backends.rerank.jina import JinaReranker
from opensac.backends.search import (
    RetrievalMetadata,
    SearchBatch,
    SearchBatchFailure,
    SearchHit,
)
from opensac.broker.app import RpcResponse
from opensac.broker.call_context import trace_error_message
from opensac.broker.failures import CapabilityFailure
from opensac.broker.policy import BudgetExceeded, MechanismDisabled
from opensac.broker.providers import (
    CapabilityProviderError,
    InflightCapacityError,
    ProviderExecutionConfig,
)
from opensac.broker.service import BrokerService
from opensac.broker.sources import canonical_url, normalize_web_source
from opensac.models import (
    CAPABILITY_METHODS,
    Mechanisms,
    ResourceBudget,
    Session,
)
from opensac.provider import ProviderPolicy, ProviderRequestError, ProviderRuntime

COALESCING = ProviderExecutionConfig(inflight_coalescing=True)


def test_rpc_response_separates_result_and_error() -> None:
    failure = CapabilityFailure(
        code="provider_unavailable",
        message="Provider is unavailable.",
        retryable=True,
    )

    successful = RpcResponse(ok=True, result={"value": 1})
    assert successful.error is None
    assert successful.capability_contract == 13
    assert RpcResponse(ok=False, error=failure).result is None
    with pytest.raises(ValidationError, match="cannot contain an error"):
        RpcResponse(ok=True, result={}, error=failure)
    with pytest.raises(ValidationError, match="must contain an error"):
        RpcResponse(ok=False)
    with pytest.raises(ValidationError, match="cannot contain a result"):
        RpcResponse(ok=False, result={}, error=failure)


class _LocalBackendTraits:
    source_kind = "opaque"

    @staticmethod
    def fetch_candidates(hit: DocumentHandle) -> list[DocumentHandle]:
        return [hit]


def _broker_service(search_backends, *, document_backends=None, **kwargs):
    if document_backends is None:
        document_backends = search_backends
    return BrokerService(
        search_backends,
        document_backends=document_backends,
        **kwargs,
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

    @property
    def source_kind(self) -> str:
        return "public_url" if self.name == "web" else "opaque"

    @staticmethod
    def fetch_candidates(hit: DocumentHandle) -> list[DocumentHandle]:
        return [hit]

    def _hit(self, query: str, rank: int) -> SearchHit:
        return SearchHit(
            source="",
            backend=self.name,
            title=query,
            url=f"https://example.com/{rank}" if self.source_kind == "public_url" else None,
            docid=str(rank) if self.source_kind == "opaque" else None,
            snippet="snippet",
            rank=rank,
        )

    async def search(self, query, *, limit, offset=0, domains=None):
        ranks = range(offset + 1, min(offset + limit, self.depth) + 1)
        return [self._hit(query, rank) for rank in ranks]

    async def fetch(self, hit, *, query=None):
        return DocumentContent(source=hit.source, text=f"content:{query}", url=hit.url)


class BrokenBackend:
    name = "web"
    source_kind = "public_url"
    supports_domains = True
    max_depth = None

    async def search(self, query, *, limit, offset=0, domains=None):
        raise RuntimeError("backend exploded")

    async def fetch(self, hit, *, query=None):
        del hit, query
        raise RuntimeError("backend exploded")

    @staticmethod
    def fetch_candidates(hit: DocumentHandle) -> list[DocumentHandle]:
        return [hit]


class PassageCorpusBackend:
    """Frozen in-memory web pages with observable per-document fetches."""

    name = "web"
    source_kind = "public_url"
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
                source="",
                backend="web",
                title=f"Frozen page {index}",
                url=f"https://example.test/{index}",
                date=f"202{index}",
                snippet="frozen",
                rank=index + 1,
            )
            for index in range(offset, min(offset + limit, len(self.documents)))
        ]

    async def fetch(self, hit, *, query=None):
        del query
        index = int(str(hit.url).rsplit("/", 1)[-1])
        self.fetched.append(index)
        if index in self.fail:
            raise ProviderRequestError(
                "provider_rejected",
                "Provider rejected one document.",
                retryable=False,
            )
        return DocumentContent(
            source=hit.source,
            text=self.documents[index],
            title=hit.title,
            url=hit.url,
            date=hit.date,
        )

    @staticmethod
    def fetch_candidates(hit: DocumentHandle) -> list[DocumentHandle]:
        return [hit]


def make_session(*, backends=None, mechanisms=None, budget=None):
    return Session(
        id="sess_test",
        token="token",
        backends=backends or ["web"],
        workspace="/tmp/session",
        mechanisms=mechanisms or Mechanisms(),
        budget=budget or ResourceBudget(),
    )


def test_broker_requires_explicit_matching_document_backends() -> None:
    with pytest.raises(TypeError, match="document_backends"):
        BrokerService({"web": FakeBackend("web")})  # type: ignore[call-arg]

    with pytest.raises(ValueError, match="must match exactly"):
        BrokerService(
            {"web": FakeBackend("web")},
            document_backends={"local": FakeBackend("local")},
        )


def test_broker_rejects_unsupported_source_kinds_at_startup() -> None:
    class UnsupportedSourceKind(FakeBackend):
        source_kind = "filesystem"

    with pytest.raises(ValueError, match="invalid source kind 'filesystem'"):
        _broker_service({"custom": UnsupportedSourceKind("custom")})


async def test_search_and_content_use_distinct_backend_objects() -> None:
    class SearchOnly:
        name = "web"
        supports_domains = True
        max_depth = 100
        provider_identity = "search-only"

        def __init__(self) -> None:
            self.calls = 0

        async def search(self, query, *, limit, offset=0, domains=None):
            del query, domains
            self.calls += 1
            return [
                SearchHit(
                    backend="web",
                    url="https://example.com/separate",
                    rank=offset + 1,
                )
            ][:limit]

    class DocumentOnly:
        name = "web"
        source_kind = "public_url"
        provider_identity = "document-only"

        def __init__(self) -> None:
            self.calls = 0

        async def fetch(self, hit, *, query=None):
            del query
            self.calls += 1
            return DocumentContent(source=hit.source, text="separate document", url=hit.url)

        @staticmethod
        def fetch_candidates(hit: DocumentHandle) -> list[DocumentHandle]:
            return [hit]

    search_backend = SearchOnly()
    document_backend = DocumentOnly()
    service = BrokerService(
        {"web": search_backend},
        document_backends={"web": document_backend},
    )
    service.register_session(make_session())

    hit = (await service.call("token", "search.query", {"query": "separate"}))[0]
    content = await service.call("token", "content.read", {"source": hit["source"]})

    assert content["text"] == "separate document"
    assert search_backend.calls == 1
    assert document_backend.calls == 1


async def test_broker_scopes_sources_and_fetches_content() -> None:
    service = _broker_service({"web": FakeBackend("web")})
    service.register_session(make_session())
    hits = await service.call("token", "search.query", {"query": "query", "limit": 3})
    assert hits[0]["source"] == "https://example.com/1"
    assert "ref" not in hits[0]
    assert "url" not in hits[0]
    content = await service.call(
        "token",
        "content.read",
        {"source": hits[0]["source"]},
    )
    assert content["text"] == "content:None"


def test_source_collision_does_not_mutate_session_identity_map() -> None:
    service = _broker_service({"web": FakeBackend("web")})
    state = service.register_session(make_session())
    source = "https://example.com/document"
    first = DocumentHandle(source=source, url=source, docid="one")
    second = DocumentHandle(source=source, url=source, docid="two")

    assert state.remember("web", first, identity="web:docid:one") == source
    with pytest.raises(ValueError, match="multiple documents"):
        state.remember("web", second, identity="web:docid:two")

    assert state.document_id_by_alias == {source: "web:docid:one"}
    assert state.document_id_by_backend_identity == {"web:docid:one": "web:docid:one"}
    assert state.documents_by_id["web:docid:one"].handle == first


async def test_broker_rejects_legacy_source_parameters_and_removed_citations() -> None:
    service = _broker_service({"web": FakeBackend("web")})
    service.register_session(make_session())

    with pytest.raises(ValueError, match="legacy content"):
        await service.call("token", "content.fetch", {"refs": []})
    with pytest.raises(ValueError, match="legacy content"):
        await service.call(
            "token",
            "content.passages",
            {"query": "evidence", "sources": [], "max_per_ref": 1},
        )
    with pytest.raises(ValueError, match="Unsupported capability"):
        await service.call("token", "citations.resolve", {"requests": []})


async def test_search_fails_loudly_when_the_session_backend_is_not_configured() -> None:
    """The one thing resolution must never do is pick something else.

    `search.query` carries no backend name, so a session pointed at a backend
    this broker does not have has to stop here. Falling through to whatever is
    configured would run the whole question against the wrong corpus and report
    a score for it.
    """
    service = _broker_service({"local": FakeBackend("local")})
    service.register_session(make_session(backends=["web"]))
    with pytest.raises(RuntimeError, match="exactly one configured search backend"):
        await service.call("token", "search.query", {"query": "query"})


async def test_failed_search_consumes_hard_budget_before_backend_side_effect() -> None:
    service = _broker_service({"web": BrokenBackend()})
    state = service.register_session(make_session(budget=ResourceBudget(max_search_queries=1)))

    with pytest.raises(ProviderRequestError) as failed:
        await service.call("token", "search.query", {"query": "first"}, execution_id="exec-1")
    assert failed.value.code == "provider_invalid_response"
    assert failed.value.attempts == 1
    with pytest.raises(BudgetExceeded, match="max_search_queries"):
        await service.call("token", "search.query", {"query": "retry"}, execution_id="exec-1")

    assert state.policy.usage.search_calls == 1
    assert state.policy.remaining()["max_search_queries"] == 0
    assert state.policy.terminal_reason == "budget_exhausted:max_search_queries"
    trace = service.take_trace("token", "exec-1")
    assert [event.error_type for event in trace] == [
        "ProviderRequestError",
        "BudgetExceeded",
    ]


async def test_search_service_rejects_invalid_backend_output() -> None:
    class InvalidSearchBackend(FakeBackend):
        async def search(self, query, *, limit, offset=0, domains=None):
            del query, limit, offset, domains
            return [{"backend": "web", "rank": True}]

    service = _broker_service({"web": InvalidSearchBackend("web")})
    service.register_session(make_session())

    with pytest.raises(ProviderRequestError) as failed:
        await service.call(
            "token",
            "search.query",
            {"query": "invalid backend output"},
            execution_id="invalid-search-output",
        )

    assert failed.value.code == "provider_invalid_response"
    assert failed.value.attempts == 1
    attempt = service.take_trace("token", "invalid-search-output")[0].provider_attempts[0]
    assert (attempt.component, attempt.status) == ("search", "error")


async def test_document_service_rejects_invalid_backend_output() -> None:
    class InvalidDocumentBackend(FakeBackend):
        async def fetch(self, hit, *, query=None):
            del query
            return {"source": hit.source, "text": 42}

    service = _broker_service({"web": InvalidDocumentBackend("web")})
    service.register_session(make_session())
    source = (await service.call("token", "search.query", {"query": "source"}))[0]["source"]

    with pytest.raises(CapabilityProviderError) as failed:
        await service.call(
            "token",
            "content.fetch",
            {"source": source},
            execution_id="invalid-document-output",
        )

    assert failed.value.code == "provider_invalid_response"
    attempt = service.take_trace("token", "invalid-document-output")[0].provider_attempts[0]
    assert (attempt.component, attempt.status) == ("document", "error")


async def test_document_service_rejects_candidate_that_changes_authorized_source() -> None:
    class InvalidCandidateBackend(FakeBackend):
        @staticmethod
        def fetch_candidates(hit: DocumentHandle) -> list[DocumentHandle]:
            return [hit.model_copy(update={"source": "https://attacker.invalid/other"})]

    service = _broker_service({"web": InvalidCandidateBackend("web")})
    service.register_session(make_session())
    source = (await service.call("token", "search.query", {"query": "source"}))[0]["source"]

    with pytest.raises(CapabilityProviderError) as failed:
        await service.call(
            "token",
            "content.fetch",
            {"source": source},
            execution_id="invalid-document-candidate",
        )

    assert failed.value.code == "provider_invalid_response"
    assert failed.value.attempts == 0
    assert failed.value.component == "document"
    trace = service.take_trace("token", "invalid-document-candidate")[0]
    assert trace.provider_attempts == []


async def test_concurrent_search_budget_reservations_never_overspend() -> None:
    class CountingBackend(FakeBackend):
        def __init__(self) -> None:
            super().__init__("web")
            self.calls = 0

        async def search(self, query, *, limit, offset=0, domains=None):
            self.calls += 1
            await asyncio.sleep(0)
            return await super().search(query, limit=limit, offset=offset, domains=domains)

    backend = CountingBackend()
    service = _broker_service({"web": backend})
    state = service.register_session(make_session(budget=ResourceBudget(max_search_queries=1)))

    results = await asyncio.gather(
        *(service.call("token", "search.query", {"query": f"query-{index}"}) for index in range(8)),
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
    service = _broker_service({"local": FakeBackend("local")})
    service.register_session(make_session(backends=["local"]))
    with pytest.raises(ValueError, match="no domain filter"):
        await service.call(
            "token", "search.query", {"query": "q", "include_domains": ["example.com"]}
        )
    # The same call without the argument is fine, so the refusal is about the
    # parameter rather than about the backend.
    assert await service.call("token", "search.query", {"query": "q"})


async def test_search_refuses_depth_the_backend_cannot_serve() -> None:
    """Enforced centrally so every backend refuses in the same words."""

    class Shallow(FakeBackend):
        max_depth = 100

    service = _broker_service({"web": Shallow("web", depth=200)})
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
            return await super().search(query, limit=limit, offset=offset, domains=domains)

    backend = Counting()
    service = _broker_service(
        {"web": backend},
        max_search_query_chars=4,
        max_search_top_k=20,
    )
    state = service.register_session(make_session())

    with pytest.raises(ValueError, match="5 characters"):
        await service.call("token", "search.query", {"query": "abcde"})
    with pytest.raises(ValueError, match="retrieval depth 21"):
        await service.call("token", "search.query", {"query": "ok", "limit": 10, "offset": 11})
    with pytest.raises(ValueError, match="domains must be a list"):
        await service.call(
            "token", "search.query", {"query": "ok", "include_domains": "example.com"}
        )

    assert backend.calls == 0
    assert state.policy.usage.search_calls == 0


async def test_search_many_rejects_hard_budgets_before_fanout() -> None:
    service = _broker_service(
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
    report = await service.call("token", "search.query_many", {"queries": ["ok", "abcde"]})
    assert report["results"][0]["input_index"] == 0
    assert report["failures"][0]["input_index"] == 1
    assert report["failures"][0]["code"] == "invalid_request"
    assert report["failures"][0]["attempts"] == 0
    with pytest.raises(ValueError, match="retrieval depth 21"):
        await service.call(
            "token",
            "search.query_many",
            {"queries": ["ok"], "limit": 10, "offset": 11},
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
    service = _broker_service({"web": FakeBackend("web")})
    state = service.register_session(make_session())
    for index in range(25):
        await service.call("token", "search.query", {"query": f"q{index}"})
    assert state.policy.usage.search_calls == 25


async def test_search_many_returns_aligned_failures_when_every_query_fails() -> None:
    service = _broker_service({"web": BrokenBackend()})
    service.register_session(make_session())
    report = await service.call("token", "search.query_many", {"queries": ["one", "two"]})
    assert report["results"] == []
    assert [row["input_index"] for row in report["failures"]] == [0, 1]
    assert [row["code"] for row in report["failures"]] == [
        "provider_invalid_response",
        "provider_invalid_response",
    ]
    assert [row["attempts"] for row in report["failures"]] == [1, 1]


async def test_search_many_tolerates_partial_failure() -> None:
    service = _broker_service({"web": FakeBackend("web")})
    service.register_session(make_session())
    # An empty query is rejected by the broker while the other one succeeds.
    report = await service.call("token", "search.query_many", {"queries": ["ok", ""]})
    assert len(report["results"][0]["hits"]) == 1
    assert report["results"][0]["input_index"] == 0
    assert "must not be empty" in report["failures"][0]["message"]
    assert report["failures"][0] == {
        "input_index": 1,
        "query": "",
        "code": "invalid_request",
        "message": "query must not be empty",
        "retryable": False,
        "attempts": 0,
        "provider_status": None,
        "retry_after_seconds": None,
        "provider": "web",
        "component": "search",
        "scope": "request",
    }


async def test_web_search_many_registers_provenance_in_input_order() -> None:
    class CompletionOrderBackend(FakeBackend):
        async def search(self, query, *, limit, offset=0, domains=None):
            if query == "first":
                await asyncio.sleep(0.02)
            return [
                SearchHit(
                    source="",
                    backend="web",
                    title=query,
                    url="https://example.com/shared",
                    snippet=query,
                    rank=1,
                    retrieval=RetrievalMetadata(mode="organic", result_mode="snippet"),
                )
            ]

    service = _broker_service({"web": CompletionOrderBackend("web")})
    state = service.register_session(make_session())

    report = await service.call(
        "token",
        "search.query_many",
        {"queries": ["first", "second"], "concurrency": 2},
        execution_id="exec-query-order",
    )

    results = report["results"]
    assert results[0]["hits"][0]["source"] == results[1]["hits"][0]["source"]
    record = state.document_for_alias(results[0]["hits"][0]["source"])
    assert record is not None and record.handle.title == "first"
    event = service.take_trace("token", "exec-query-order")[0]
    assert [hit.query_index for hit in event.hits] == [0, 1]
    assert [hit.retrieval_mode for hit in event.hits] == ["organic", "organic"]


async def test_custom_route_search_many_prefers_backend_batch_and_preserves_order() -> None:
    class BatchCustom(FakeBackend):
        source_kind = "opaque"

        def __init__(self) -> None:
            super().__init__("custom")
            self.batch_calls: list[list[str]] = []
            self.single_calls = 0

        async def search(self, query, *, limit, offset=0, domains=None):
            self.single_calls += 1
            return await super().search(query, limit=limit, offset=offset, domains=domains)

        async def search_many(self, queries, *, limit, offset=0, domains=None):
            self.batch_calls.append(list(queries))
            return [
                SearchBatch(query=query, hits=[self._hit(query, offset + 1)]) for query in queries
            ]

    backend = BatchCustom()
    service = _broker_service({"custom": backend})
    state = service.register_session(make_session(backends=["custom"]))

    report = await service.call(
        "token",
        "search.query_many",
        {"queries": ["second", "", "first"], "limit": 1},
        execution_id="custom-batch",
    )

    assert backend.batch_calls == [["second", "first"]]
    assert backend.single_calls == 0
    assert [batch["query"] for batch in report["results"]] == ["second", "first"]
    assert [batch["input_index"] for batch in report["results"]] == [0, 2]
    assert [
        report["results"][0]["hits"][0]["title"],
        report["results"][1]["hits"][0]["title"],
    ] == [
        "second",
        "first",
    ]
    assert report["failures"][0]["input_index"] == 1
    assert "must not be empty" in report["failures"][0]["message"]
    assert state.policy.usage.search_calls == 3
    attempts = service.take_trace("token", "custom-batch")[0].provider_attempts
    assert {attempt.component for attempt in attempts} == {"search"}


async def test_custom_route_without_batch_uses_single_requests_and_default_policy() -> None:
    class CustomSearch(FakeBackend):
        source_kind = "opaque"

        def __init__(self) -> None:
            super().__init__("custom")
            self.single_calls = 0

        async def search(self, query, *, limit, offset=0, domains=None):
            self.single_calls += 1
            return await super().search(query, limit=limit, offset=offset, domains=domains)

    backend = CustomSearch()
    policy = ProviderPolicy(concurrency=3)
    runtime = ProviderRuntime(policy)
    service = _broker_service(
        {"custom": backend},
        search_runtime=runtime,
    )
    service.register_session(make_session(backends=["custom"]))

    report = await service.call(
        "token",
        "search.query_many",
        {"queries": ["one", "two"]},
        execution_id="custom-single",
    )

    assert [row["query"] for row in report["results"]] == ["one", "two"]
    assert backend.single_calls == 2
    assert runtime.policy is policy
    attempts = service.take_trace("token", "custom-single")[0].provider_attempts
    assert {attempt.component for attempt in attempts} == {"search"}


async def test_broker_closes_each_backend_instance_once() -> None:
    class Closable(FakeBackend):
        def __init__(self) -> None:
            super().__init__("local")
            self.close_calls = 0

        async def aclose(self) -> None:
            self.close_calls += 1

    class ClosableLLM:
        def __init__(self) -> None:
            self.close_calls = 0

        async def aclose(self) -> None:
            self.close_calls += 1

    search_backend = Closable()
    document_backend = Closable()
    llm_backend = ClosableLLM()
    service = _broker_service(
        {"local": search_backend, "same-instance": search_backend},
        document_backends={"local": document_backend, "same-instance": document_backend},
        llm_backend=llm_backend,
    )

    await service.aclose()

    assert search_backend.close_calls == 1
    assert document_backend.close_calls == 1
    assert llm_backend.close_calls == 1


class FakeModelClient:
    """Minimal stand-in for the AsyncOpenAI surface used by the LLM backend."""

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
    service = _broker_service(
        {"web": FakeBackend("web")},
        llm_backend=OpenAICompatibleBackend(model="test-model", client=client),
        **limits,
    )
    service.register_session(make_session(budget=budget))
    return service, client


def make_llm_service() -> tuple[BrokerService, FakeModelClient]:
    client = FakeModelClient()
    service = _broker_service(
        {"web": FakeBackend("web")},
        llm_backend=OpenAICompatibleBackend(model="test-model", client=client),
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
        execution_id="llm-complete",
    )
    assert answer == "echo:plan the next queries"
    assert client.calls[0]["messages"][0] == {"role": "system", "content": "be terse"}
    assert client.calls[0]["temperature"] == 0.7
    # complete() is free-form, so it must not force JSON mode the way extract does.
    assert "response_format" not in client.calls[0]
    assert service.sessions["token"].policy.usage.llm_calls == 1
    assert service.sessions["token"].policy.usage.pipeline_model_tokens == 11
    assert service.sessions["token"].policy.usage.provider_attempts_by_capability == {"llm": 1}
    assert service.llm.service is service.llm_service
    trace = service.take_trace("token", "llm-complete")[0]
    assert [
        (attempt.component, attempt.request_indexes) for attempt in trace.provider_attempts
    ] == [("llm", [0])]


async def test_llm_service_rejects_invalid_backend_output() -> None:
    class InvalidLLMBackend:
        name = "test:invalid"
        provider_identity = "test:invalid"

        async def complete(
            self,
            prompt,
            *,
            system=None,
            temperature=None,
            max_tokens=None,
            json_object=False,
        ):
            del prompt, system, temperature, max_tokens, json_object
            return {"content": "answer", "tokens": True}

    service = _broker_service(
        {"web": FakeBackend("web")},
        llm_backend=InvalidLLMBackend(),
    )
    service.register_session(make_session())

    with pytest.raises(ProviderRequestError) as failed:
        await service.call(
            "token",
            "llm.complete",
            {"prompt": "invalid backend output"},
            execution_id="invalid-llm-output",
        )

    assert failed.value.code == "provider_invalid_response"
    assert failed.value.attempts == 1
    attempt = service.take_trace("token", "invalid-llm-output")[0].provider_attempts[0]
    assert (attempt.component, attempt.status) == ("llm", "error")


async def test_llm_service_applies_runtime_retry_and_reports_capacity() -> None:
    client = ScriptedModelClient([httpx.ReadTimeout("provider timeout"), "recovered"])
    runtime = ProviderRuntime(
        ProviderPolicy(
            retry_profile="safe",
            max_attempts=2,
            base_backoff_seconds=0,
            max_backoff_seconds=0,
            max_total_backoff_seconds=0,
            concurrency=1,
        )
    )
    service = _broker_service(
        {"web": FakeBackend("web")},
        llm_backend=OpenAICompatibleBackend(model="test-model", client=client),
        llm_runtime=runtime,
    )
    state = service.register_session(make_session())

    answer = await service.call(
        "token",
        "llm.complete",
        {"prompt": "retry me"},
        execution_id="llm-retry",
    )

    assert answer == "recovered"
    assert len(client.calls) == 2
    assert state.policy.usage.provider_attempts_by_capability == {"llm": 2}
    assert state.policy.usage.provider_retries == 1
    assert service.provider_service_snapshot()["llm"] == {
        "capacity": 1,
        "active": 0,
        "waiting": 0,
        "admitted": 2,
    }
    attempts = service.take_trace("token", "llm-retry")[0].provider_attempts
    assert [(attempt.component, attempt.status) for attempt in attempts] == [
        ("llm", "error"),
        ("llm", "success"),
    ]


async def test_pipeline_output_budget_clamps_and_reserves_before_call() -> None:
    client = FakeModelClient()
    service = _broker_service(
        {"web": FakeBackend("web")},
        llm_backend=OpenAICompatibleBackend(model="test-model", client=client),
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
    service = _broker_service({"web": FakeBackend("web")})
    service.register_session(make_session())
    await service.call(
        "token",
        "search.query_many",
        {"queries": ["one", "two"], "limit": 1},
        execution_id="exec-1",
    )
    with pytest.raises(ValueError):
        await service.call(
            "token",
            "content.grep",
            {"sources": ["missing"], "pattern": ""},
            execution_id="exec-1",
        )

    trace = service.take_trace("token", "exec-1")
    assert [event.method for event in trace] == ["search.query_many", "content.grep"]
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
        {"query": "", "sources": []},
        {"query": None, "sources": []},
        {"query": "q", "sources": [], "limit": 0},
        {"query": "q", "sources": [], "limit": "invalid"},
        {"query": "q", "sources": [], "limit": True},
        {"query": "q", "sources": [], "limit": 101},
        {"query": "q", "sources": [], "limit_per_source": 0},
        {"query": "q", "sources": [], "limit_per_source": None},
        {"query": "q", "sources": [], "limit_per_source": 11},
    ],
)
async def test_passages_reject_invalid_public_parameters(params) -> None:
    service = _broker_service({"web": PassageCorpusBackend([])})
    service.register_session(make_session())

    with pytest.raises(ValueError):
        await service.call("token", "content.passages", params)


async def test_passages_empty_sources_and_exact_duplicates_are_successful() -> None:
    backend = PassageCorpusBackend(["alpha evidence", "beta evidence"])
    service = _broker_service({"web": backend})
    state = service.register_session(make_session())
    hits = await service.call("token", "search.query", {"query": "seed", "limit": 2})

    empty = await service.call(
        "token",
        "content.passages",
        {"query": "evidence", "sources": []},
    )
    report = await service.call(
        "token",
        "content.passages",
        {
            "query": "evidence",
            "sources": [hits[0]["source"], hits[0]["source"], hits[1]["source"]],
            "limit": 2,
            "limit_per_source": 1,
        },
    )

    assert empty == {
        "query": "evidence",
        "passages": [],
        "failures": [],
        "warnings": [],
        "input_count": 0,
        "unique_source_count": 0,
    }
    assert report["input_count"] == 3
    assert report["unique_source_count"] == 2
    assert [row["source"] for row in report["passages"]] == [
        hits[0]["source"],
        hits[1]["source"],
    ]
    assert backend.fetched == [0, 1]
    assert state.policy.usage.content_fetches == 2
    assert state.policy.usage.intra_call_deduplicated_items == 1


async def test_passages_apply_source_limit_before_deduplication() -> None:
    backend = PassageCorpusBackend(["alpha"])
    service = _broker_service({"web": backend}, max_content_sources_per_request=2)
    service.register_session(make_session())
    source = (await service.call("token", "search.query", {"query": "seed"}))[0]["source"]

    with pytest.raises(ValueError, match="maximum of 2"):
        await service.call(
            "token",
            "content.passages",
            {"query": "alpha", "sources": [source, source, source]},
        )


async def test_passages_bm25_supports_english_and_chinese_queries() -> None:
    backend = PassageCorpusBackend(
        [
            "The audited report states that Singapore revenue reached 42 million dollars.",
            "公司公告显示，新加坡营收达到四千二百万美元。",
        ]
    )
    service = _broker_service({"web": backend})
    service.register_session(make_session())
    hits = await service.call("token", "search.query", {"query": "seed", "limit": 2})
    sources = [hit["source"] for hit in hits]

    english = await service.call(
        "token",
        "content.passages",
        {"query": "Singapore revenue", "sources": sources, "limit": 1},
        execution_id="passages-lexical",
    )
    chinese = await service.call(
        "token",
        "content.passages",
        {"query": "新加坡 营收", "sources": sources, "limit": 1},
    )

    assert english["passages"][0]["source"] == sources[0]
    assert chinese["passages"][0]["source"] == sources[1]
    assert english["passages"][0]["ranker"] == "lexical:bm25"
    assert chinese["passages"][0]["score"] > 0
    trace = service.take_trace("token", "passages-lexical")[0]
    rerank_attempts = [
        attempt for attempt in trace.provider_attempts if attempt.component == "rerank"
    ]
    assert [(attempt.status, attempt.request_indexes) for attempt in rerank_attempts] == [
        ("success", [0, 1])
    ]


async def test_passages_stably_break_ties_and_apply_limit_per_source_after_ranking() -> None:
    backend = PassageCorpusBackend(["a" * 180, "b" * 180])
    service = _broker_service(
        {"web": backend},
        passage_chunk_chars=50,
        passage_chunk_overlap_chars=10,
    )
    service.register_session(make_session())
    hits = await service.call("token", "search.query", {"query": "seed", "limit": 2})
    sources = [hit["source"] for hit in hits]

    first = await service.call(
        "token",
        "content.passages",
        {"query": "unmatched", "sources": sources, "limit": 4, "limit_per_source": 2},
    )
    second = await service.call(
        "token",
        "content.passages",
        {"query": "unmatched", "sources": sources, "limit": 4, "limit_per_source": 2},
    )

    assert first["passages"] == second["passages"]
    assert [row["source"] for row in first["passages"]] == [
        sources[0],
        sources[0],
        sources[1],
        sources[1],
    ]
    assert [row["rank"] for row in first["passages"]] == [1, 2, 3, 4]
    assert [row["coordinates"]["start_character"] for row in first["passages"][:2]] == [
        0,
        40,
    ]


async def test_passages_keep_partial_fetch_failures() -> None:
    backend = PassageCorpusBackend(
        ["alpha answer", "unavailable", "alpha corroboration"],
        fail={1},
    )
    service = _broker_service({"web": backend})
    service.register_session(make_session())
    hits = await service.call("token", "search.query", {"query": "seed", "limit": 3})
    sources = [hit["source"] for hit in hits]

    report = await service.call(
        "token",
        "content.passages",
        {"query": "alpha", "sources": sources, "limit": 2, "limit_per_source": 1},
        execution_id="passages-partial",
    )

    assert report["failures"][0]["input_index"] == 1
    assert report["failures"][0]["source"] == sources[1]
    assert report["failures"][0]["code"] == "provider_rejected"
    assert [row["source"] for row in report["passages"]] == [sources[0], sources[2]]
    assert all("locator" not in row for row in report["passages"])
    trace = service.take_trace("token", "passages-partial")[0]
    assert trace.result_count == 2
    assert len(trace.passage_records) == 2
    assert "alpha answer" not in trace.model_dump_json()
    assert trace.passage_records[0].coordinates == report["passages"][0]["coordinates"]


async def test_passage_reranker_maps_scores_by_index_even_when_results_are_unordered() -> None:
    class UnorderedReranker:
        name = "test:unordered"
        provider_identity = "test:unordered"

        def preflight(self) -> None:
            return None

        async def rerank(self, query, documents):
            del query
            return [
                RerankScore(index=1, score=9.0),
                RerankScore(index=0, score=1.0),
            ][: len(documents)]

    backend = PassageCorpusBackend(["alpha first", "alpha second"])
    service = _broker_service({"web": backend}, reranker=UnorderedReranker())
    service.register_session(make_session())
    hits = await service.call("token", "search.query", {"query": "seed", "limit": 2})

    report = await service.call(
        "token",
        "content.passages",
        {
            "query": "alpha",
            "sources": [hit["source"] for hit in hits],
            "limit": 2,
            "limit_per_source": 1,
        },
        execution_id="passages-rerank",
    )

    assert [row["source"] for row in report["passages"]] == [hits[1]["source"], hits[0]["source"]]
    assert [row["score"] for row in report["passages"]] == [9.0, 1.0]
    trace = service.take_trace("token", "passages-rerank")[0]
    rerank_attempts = [
        attempt for attempt in trace.provider_attempts if attempt.component == "rerank"
    ]
    assert rerank_attempts[0].request_indexes == [0, 1]


async def test_invalid_reranker_result_falls_back_with_rerank_attempt_count() -> None:
    class IncompleteReranker:
        name = "test:incomplete"
        provider_identity = "test:incomplete"

        def preflight(self) -> None:
            return None

        async def rerank(self, query, documents):
            del query, documents
            return []

    backend = PassageCorpusBackend(["alpha first"])
    service = _broker_service({"web": backend}, reranker=IncompleteReranker())
    service.register_session(make_session())
    source = (await service.call("token", "search.query", {"query": "seed"}))[0]["source"]

    report = await service.call(
        "token",
        "content.passages",
        {"query": "alpha", "sources": [source]},
    )

    assert report["passages"][0]["ranker"] == "lexical:bm25"
    assert report["warnings"][0]["code"] == "provider_invalid_response"
    assert report["warnings"][0]["attempts"] == 1


async def test_passage_prefilter_keeps_eight_per_source_then_caps_globally_at_100() -> None:
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
            return [RerankScore(index=index, score=0.0) for index in range(len(documents))]

    reranker = CapturingReranker()
    backend = PassageCorpusBackend([chr(97 + index) * 200 for index in range(13)])
    service = _broker_service(
        {"web": backend},
        reranker=reranker,
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
            "sources": [hit["source"] for hit in hits],
            "limit": 1,
            "limit_per_source": 3,
        },
    )

    counts = Counter(document[0] for document in reranker.documents)
    assert len(reranker.documents) == 100
    assert [counts[chr(97 + index)] for index in range(13)] == [*([8] * 12), 4]


async def test_jina_mode_falls_back_on_failure_but_empty_pages_need_no_warning() -> None:
    empty = _broker_service(
        {"web": PassageCorpusBackend([""])},
        reranker=JinaReranker(),
    )
    empty.register_session(make_session())
    empty_source = (await empty.call("token", "search.query", {"query": "seed"}))[0]["source"]
    report = await empty.call(
        "token",
        "content.passages",
        {"query": "answer", "sources": [empty_source]},
    )
    assert report["passages"] == []

    configured = _broker_service(
        {"web": PassageCorpusBackend(["lexically matching answer"])},
        reranker=JinaReranker(),
    )
    configured.register_session(make_session())
    source = (await configured.call("token", "search.query", {"query": "seed"}))[0]["source"]
    fallback = await configured.call(
        "token",
        "content.passages",
        {"query": "answer", "sources": [source]},
        execution_id="passages-missing-jina",
    )

    assert fallback["passages"][0]["ranker"] == "lexical:bm25"
    assert fallback["warnings"][0]["code"] == "provider_not_configured"
    assert fallback["warnings"][0]["attempts"] == 0
    assert fallback["warnings"][0]["provider"] == "jina_reranker"
    assert fallback["warnings"][0]["component"] == "rerank"
    assert fallback["warnings"][0]["scope"] == "provider"
    trace = configured.take_trace("token", "passages-missing-jina")[0]
    assert trace.status == "ok"
    assert not any(attempt.component == "rerank" for attempt in trace.provider_attempts)


async def test_extract_returns_one_checked_json_object() -> None:
    service, _ = make_scripted_llm_service(
        ['{"status":"ok","note":null,"tags":["a"],"details":{"count":2}}']
    )
    result = await service.call(
        "token",
        "llm.extract",
        {
            "item": "input",
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
        execution_id="exec-extract",
    )

    assert result["details"] == {"count": 2}
    state = service.sessions["token"]
    assert state.policy.usage.llm_calls == 1
    assert state.policy.usage.pipeline_model_tokens == 7
    event = service.take_trace("token", "exec-extract")[0]
    assert [
        (attempt.index, attempt.phase, attempt.error_code) for attempt in event.model_attempts
    ] == [(0, "initial", None)]
    assert event.result_payload is None


@pytest.mark.parametrize(
    ("outcome", "code"),
    [
        ("[]", "non_object"),
        ('{"name":"first","name":"duplicate"}', "invalid_json"),
        ('{"name":NaN}', "invalid_json"),
        ('{"name":1e400}', "invalid_json"),
        ('{"name":1}', "schema_mismatch"),
        ("   ", "empty_output"),
    ],
)
async def test_extract_promotes_invalid_model_output_to_top_level_error(
    outcome: str,
    code: str,
) -> None:
    service, _ = make_scripted_llm_service([outcome])

    with pytest.raises(CapabilityProviderError) as failed:
        await service.call(
            "token",
            "llm.extract",
            {
                "item": 1,
                "instruction": "Extract a name",
                "schema": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                    "additionalProperties": False,
                },
            },
        )

    assert failed.value.code == code
    assert failed.value.attempts == 1


async def test_extract_validates_schema_and_limits_before_charging() -> None:
    service, client = make_scripted_llm_service(
        ['{"ok":true}'],
        max_extract_instruction_bytes=3,
        max_extract_schema_bytes=50,
        max_extract_item_bytes=4,
        max_extract_schema_depth=2,
    )
    state = service.sessions["token"]
    cases = [
        (
            {
                "item": 1,
                "instruction": "",
                "schema": {"type": "object", "properties": {"ok": {"type": bool}}},
            },
            "JSON-serializable",
        ),
        (
            {
                "item": 1,
                "instruction": "",
                "schema": {"type": "object", "properties": {"ok": {"type": "string"}}},
            },
            "schema is",
        ),
        (
            {"item": 1, "instruction": "four", "schema": {"type": "object"}},
            "instruction is 4 bytes",
        ),
        (
            {"item": "long", "instruction": "", "schema": {"type": "object"}},
            "item is 6 bytes",
        ),
        (
            {
                "item": 1,
                "instruction": "",
                "schema": {"type": "object"},
                "repair_attempts": 2,
            },
            "broker maximum of 1",
        ),
    ]
    for params, message in cases:
        with pytest.raises(ValueError, match=message):
            await service.call("token", "llm.extract", params)

    assert client.calls == []
    assert state.policy.usage.llm_calls == 0


async def test_extract_repairs_once_and_returns_repaired_object() -> None:
    service, client = make_scripted_llm_service(["not json", '{"value":1}'])
    result = await service.call(
        "token",
        "llm.extract",
        {
            "item": 1,
            "instruction": "extract",
            "schema": {
                "type": "object",
                "properties": {"value": {"type": "integer"}},
                "required": ["value"],
            },
            "repair_attempts": 1,
        },
        execution_id="exec-repair",
    )

    assert result == {"value": 1}
    assert len(client.calls) == 2
    state = service.sessions["token"]
    assert state.policy.usage.llm_calls == 2
    assert state.policy.usage.pipeline_model_tokens == 14
    event = service.take_trace("token", "exec-repair")[0]
    assert [(attempt.index, attempt.phase) for attempt in event.model_attempts] == [
        (0, "initial"),
        (0, "repair"),
    ]
    assert "Validation error:\ninvalid_json:" in client.calls[1]["messages"][-1]["content"]


async def test_extract_supports_multiple_broker_configured_repairs() -> None:
    service, client = make_scripted_llm_service(
        ["not json", '{"value":"wrong"}', '{"value":1}'],
        max_extract_repair_attempts=2,
    )

    result = await service.call(
        "token",
        "llm.extract",
        {
            "item": 1,
            "instruction": "extract",
            "schema": {
                "type": "object",
                "properties": {"value": {"type": "integer"}},
                "required": ["value"],
            },
            "repair_attempts": 2,
        },
        execution_id="exec-multiple-repairs",
    )

    assert result == {"value": 1}
    assert len(client.calls) == 3
    state = service.sessions["token"]
    assert state.policy.usage.llm_calls == 3
    event = service.take_trace("token", "exec-multiple-repairs")[0]
    assert [(attempt.phase, attempt.error_code) for attempt in event.model_attempts] == [
        ("initial", "invalid_json"),
        ("repair", "schema_mismatch"),
        ("repair", None),
    ]


async def test_extract_repair_failure_preserves_error_and_attempt_count() -> None:
    service, _ = make_scripted_llm_service(["not json", "still not json"])

    with pytest.raises(CapabilityProviderError) as failed:
        await service.call(
            "token",
            "llm.extract",
            {
                "item": 1,
                "instruction": "extract",
                "schema": {"type": "object"},
                "repair_attempts": 1,
            },
        )

    assert failed.value.code == "invalid_json"
    assert failed.value.attempts == 2


async def test_extract_provider_failure_is_top_level_and_sanitized() -> None:
    service, _ = make_scripted_llm_service([RuntimeError("provider secret response body")])

    with pytest.raises(ProviderRequestError) as failed:
        await service.call(
            "token",
            "llm.extract",
            {"item": 1, "instruction": "extract", "schema": {"type": "object"}},
            execution_id="exec-provider-down",
        )

    assert failed.value.attempts == 1
    assert "secret" not in str(failed.value)
    event = service.take_trace("token", "exec-provider-down")[0]
    assert "secret" not in event.model_dump_json()


async def test_extract_reserves_quota_before_repair() -> None:
    service, client = make_scripted_llm_service(
        ["not json", '{"value":1}'],
        budget=ResourceBudget(max_pipeline_llm_calls=1),
    )

    with pytest.raises(BudgetExceeded, match="max_pipeline_llm_calls"):
        await service.call(
            "token",
            "llm.extract",
            {
                "item": 1,
                "instruction": "extract",
                "schema": {"type": "object"},
                "repair_attempts": 1,
            },
        )

    assert len(client.calls) == 1
    state = service.sessions["token"]
    assert state.policy.usage.llm_calls == 1
    assert state.policy.usage.pipeline_model_tokens == 7


async def test_llm_calls_fail_when_no_model_is_configured() -> None:
    service = _broker_service({"web": FakeBackend("web")})
    service.register_session(make_session())
    with pytest.raises(RuntimeError, match="not configured"):
        await service.call("token", "llm.complete", {"prompt": "hello"})


class RankedBackend:
    """Returns a fixed document set, so the same page recurs across queries."""

    name = "web"
    source_kind = "public_url"

    supports_domains = True

    max_depth = None

    def __init__(self, urls: list[str]) -> None:
        self.urls = urls

    async def search(self, query, *, limit, offset=0, domains=None):
        return [
            SearchHit(
                source="",
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

    async def fetch(self, hit, *, query=None):
        return DocumentContent(source=hit.source, text="body", url=hit.url)

    @staticmethod
    def fetch_candidates(hit: DocumentHandle) -> list[DocumentHandle]:
        return [hit]


async def test_the_same_document_keeps_one_source_across_queries() -> None:
    """Two queries surfacing one page must return one stable public address."""
    service = _broker_service({"web": RankedBackend(["https://example.com/a"])})
    state = service.register_session(make_session())

    first = await service.call("token", "search.query", {"query": "one"})
    second = await service.call("token", "search.query", {"query": "two"})

    assert first[0]["source"] == second[0]["source"]
    assert len(state.documents_by_id) == 1


async def test_web_sources_are_canonical_and_reproducible() -> None:
    """Equivalent URL spellings expose the same useful public address."""
    urls = ["https://Example.com/a?utm_source=news&id=7#section"]
    left = _broker_service({"web": RankedBackend(urls)})
    left.register_session(make_session())
    right = _broker_service({"web": RankedBackend(urls)})
    right.register_session(make_session())

    one = (await left.call("token", "search.query", {"query": "q"}))[0]["source"]
    two = (await right.call("token", "search.query", {"query": "q"}))[0]["source"]

    assert one == two
    assert one == "https://example.com/a?id=7"


async def test_local_source_is_the_document_id() -> None:
    """Local search and content share one source field instead of parallel IDs."""
    service = _broker_service({"local": FakeBackend("local")})
    service.register_session(make_session(backends=["local"]))
    hits = await service.call("token", "search.query", {"query": "q"})

    assert hits[0]["source"] == "1"
    assert "docid" not in hits[0]
    document = await service.call("token", "content.fetch", {"source": hits[0]["source"]})

    assert document["source"] == "1"


async def test_a_document_this_session_never_searched_is_still_refused() -> None:
    """Knowing a source is not authorization without a prior search hit.

    The sandbox has no network, so search remains the only admission door. If a
    docid the corpus contains were reachable without being retrieved, a program
    could walk the docid space and invalidate recall measurements.
    """
    service = _broker_service({"local": FakeBackend("local")})
    service.register_session(make_session(backends=["local"]))
    await service.call("token", "search.query", {"query": "q"})

    with pytest.raises(CapabilityProviderError) as failed:
        await service.call("token", "content.fetch", {"source": "999"})
    assert failed.value.code == "unknown_source"
    assert failed.value.attempts == 0


async def test_offset_reaches_ranks_a_bare_limit_cannot() -> None:
    """Depth is authorisation, not convenience.

    Because a source is admitted only for a returned hit, `limit` is both what a
    program can see and what it is allowed to fetch. Without an offset, a
    document at rank 15 is not merely inconvenient to reach, it is unreachable.
    """
    service = _broker_service({"local": FakeBackend("local", depth=50)})
    state = service.register_session(make_session(backends=["local"]))

    shallow = await service.call("token", "search.query", {"query": "q", "limit": 10})
    deep = await service.call("token", "search.query", {"query": "q", "limit": 10, "offset": 10})

    assert [hit["rank"] for hit in shallow] == list(range(1, 11))
    # Ranks stay absolute: the second window reports 11..20, not 1..10 again.
    assert [hit["rank"] for hit in deep] == list(range(11, 21))
    assert not {hit["source"] for hit in shallow} & {hit["source"] for hit in deep}
    # And the deeper hits are now fetchable, which is the point.
    assert state.document_for_alias(deep[0]["source"]) is not None


async def test_grep_line_numbers_are_read_offsets() -> None:
    """The two halves compose without character arithmetic.

    This is the same contract the function-calling profiles keep, and matching
    it is deliberate: it is the coordinate system the model already writes
    against, and a second convention here would be paid for in wrong offsets.
    """
    lines = [f"line {index}" for index in range(1, 41)]
    lines[24] = "the target phrase is here"

    class Paged(_LocalBackendTraits):
        name = "local"
        supports_domains = False
        max_depth = None

        async def search(self, query, *, limit, offset=0, domains=None):
            return [SearchHit(source="", backend="local", docid="d1", snippet="s", rank=1)]

        async def fetch(self, hit, *, query=None):
            return DocumentContent(source=hit.source, text="\n".join(lines))

    service = _broker_service({"local": Paged()})
    service.register_session(make_session(backends=["local"]))
    hits = await service.call("token", "search.query", {"query": "q"})
    source = hits[0]["source"]

    report = await service.call(
        "token",
        "content.grep",
        {"sources": [source], "pattern": r"target \w+", "context_lines": 1},
    )
    matches = report["matches"]
    assert len(matches) == 1
    assert matches[0]["line"] == 25
    assert matches[0]["before"] == ["line 24"]
    assert matches[0]["after"] == ["line 26"]
    assert report["pattern"] == r"target \w+"
    assert report["mode"] == "regex"
    assert report["case_sensitive"] is False
    assert report["source_results"][0]["match_count"] == 1
    assert report["source_results"][0]["scan_complete"] is True

    window = await service.call(
        "token",
        "content.read",
        {"source": source, "start_line": matches[0]["line"], "line_count": 2},
    )
    assert window["text"].splitlines()[0] == "the target phrase is here"
    assert window["window"]["start_line"] == 25
    assert window["window"]["total_lines"] == 40


async def test_read_reports_where_to_continue_and_where_to_stop() -> None:
    """The next cursor is exact and becomes None at EOF."""

    class Doc(_LocalBackendTraits):
        name = "local"
        supports_domains = False
        max_depth = None

        async def search(self, query, *, limit, offset=0, domains=None):
            return [SearchHit(source="", backend="local", docid="d1", snippet="s", rank=1)]

        async def fetch(self, hit, *, query=None):
            return DocumentContent(source=hit.source, text="\n".join("abcde"))

    service = _broker_service({"local": Doc()})
    service.register_session(make_session(backends=["local"]))
    source = (await service.call("token", "search.query", {"query": "q"}))[0]["source"]

    head = await service.call("token", "content.read", {"source": source, "line_count": 3})
    assert head["text"] == "a\nb\nc\n"
    assert head["window"]["next"] == {"start_line": 4, "start_character": 0}

    tail = await service.call(
        "token",
        "content.read",
        {"source": source, **head["window"]["next"], "line_count": 3},
    )
    assert tail["text"] == "d\ne"
    assert tail["window"]["next"] is None
    assert head["text"] + tail["text"] == "a\nb\nc\nd\ne"

    past_eof = await service.call("token", "content.read", {"source": source, "start_line": 6})
    assert past_eof["text"] == ""
    assert past_eof["window"]["start_line"] is None
    assert past_eof["window"]["next"] is None

    with pytest.raises(ValueError, match="exceeds the length"):
        await service.call(
            "token",
            "content.read",
            {"source": source, "start_line": 1, "start_character": 2},
        )
    with pytest.raises(ValueError, match="must be 0"):
        await service.call(
            "token",
            "content.read",
            {"source": source, "start_line": 6, "start_character": 1},
        )


async def test_repeated_reads_reuse_the_session_cache() -> None:
    class Doc(_LocalBackendTraits):
        name = "local"
        supports_domains = False
        max_depth = None

        def __init__(self) -> None:
            self.fetches = 0

        async def search(self, query, *, limit, offset=0, domains=None):
            return [SearchHit(source="", backend="local", docid="d1", snippet="s", rank=1)]

        async def fetch(self, hit, *, query=None):
            self.fetches += 1
            return DocumentContent(source=hit.source, text="one\ntwo\nthree\nfour")

    backend = Doc()
    service = _broker_service({"local": backend})
    state = service.register_session(make_session(backends=["local"]))
    source = (await service.call("token", "search.query", {"query": "q"}))[0]["source"]

    head = await service.call(
        "token",
        "content.read",
        {"source": source, "start_line": 1, "line_count": 2},
    )
    tail = await service.call(
        "token",
        "content.read",
        {"source": source, "start_line": 3, "line_count": 2},
    )

    assert [head["text"], tail["text"]] == ["one\ntwo\n", "three\nfour"]
    assert backend.fetches == 1
    assert state.policy.usage.content_fetches == 2
    assert state.policy.usage.content_backend_fetches == 1


async def test_read_rejects_invalid_coordinates_before_fetching() -> None:
    backend = CountingBackend()
    service = _broker_service({"local": backend})
    state = service.register_session(make_session(backends=["local"]))

    with pytest.raises(ValueError):
        await service.call(
            "token",
            "content.read",
            {"source": "1", "start_line": 0},
        )
    with pytest.raises(ValueError):
        await service.call(
            "token",
            "content.read",
            {"source": "1", "start_character": "2"},
        )

    assert backend.fetched == []
    assert state.policy.usage.content_fetches == 0


async def test_grep_literal_mode_and_malformed_regex_are_explicit() -> None:

    class Doc(_LocalBackendTraits):
        name = "local"
        supports_domains = False
        max_depth = None

        async def search(self, query, *, limit, offset=0, domains=None):
            return [SearchHit(source="", backend="local", docid="d1", snippet="s", rank=1)]

        async def fetch(self, hit, *, query=None):
            return DocumentContent(source=hit.source, text="written in C++ (1985)")

    service = _broker_service({"local": Doc()})
    service.register_session(make_session(backends=["local"]))
    source = (await service.call("token", "search.query", {"query": "q"}))[0]["source"]

    with pytest.raises(ValueError, match="valid regular expression"):
        await service.call(
            "token",
            "content.grep",
            {"sources": [source], "pattern": "C++ (", "mode": "regex"},
        )
    report = await service.call(
        "token",
        "content.grep",
        {"sources": [source], "pattern": "C++ (", "mode": "literal"},
    )
    matches = report["matches"]
    assert [match["line"] for match in matches] == [1]


async def test_grep_reports_complete_capped_and_failed_sources() -> None:
    class Docs(_LocalBackendTraits):
        name = "local"
        supports_domains = False
        max_depth = None

        async def search(self, query, *, limit, offset=0, domains=None):
            return [
                SearchHit(source="", backend="local", docid="early", snippet="s", rank=1),
                SearchHit(source="", backend="local", docid="last", snippet="s", rank=2),
            ]

        async def fetch(self, hit, *, query=None):
            text = "Target\nother" if hit.docid == "early" else "other\nTarget"
            return DocumentContent(source=hit.source, title=hit.docid or "", text=text)

    service = _broker_service({"local": Docs()})
    service.register_session(make_session(backends=["local"]))
    hits = await service.call("token", "search.query", {"query": "q", "limit": 2})
    report = await service.call(
        "token",
        "content.grep",
        {
            "sources": [hits[0]["source"], hits[1]["source"], "missing"],
            "pattern": "target",
            "mode": "literal",
            "limit_per_source": 1,
        },
    )

    assert report["pattern"] == "target"
    assert report["mode"] == "literal"
    assert report["limit_per_source"] == 1
    assert [row["match_count"] for row in report["source_results"]] == [1, 1]
    assert [row["scan_complete"] for row in report["source_results"]] == [
        False,
        True,
    ]
    assert report["failures"][0]["input_index"] == 2
    assert report["failures"][0]["code"] == "unknown_source"
    assert [match["input_index"] for match in report["matches"]] == [0, 1]

    case_sensitive = await service.call(
        "token",
        "content.grep",
        {
            "sources": [hits[0]["source"]],
            "pattern": "target",
            "mode": "literal",
            "case_sensitive": True,
        },
    )
    assert case_sensitive["matches"] == []
    assert case_sensitive["source_results"][0]["scan_complete"] is True


async def test_grep_cursor_continues_without_duplicate_or_missing_matches() -> None:
    class Docs(_LocalBackendTraits):
        name = "local"
        supports_domains = False
        max_depth = None

        async def search(self, query, *, limit, offset=0, domains=None):
            return [SearchHit(source="", backend="local", docid="d1", snippet="s", rank=1)]

        async def fetch(self, hit, *, query=None):
            return DocumentContent(source=hit.source, text="target one\nother\ntarget two")

    service = _broker_service({"local": Docs()})
    service.register_session(make_session(backends=["local"]))
    source = (await service.call("token", "search.query", {"query": "q"}))[0]["source"]

    first = await service.call(
        "token",
        "content.grep",
        {"pattern": "target", "sources": [source], "limit_per_source": 1},
    )
    cursor = first["source_results"][0]["next_start_line"]
    second = await service.call(
        "token",
        "content.grep",
        {
            "pattern": "target",
            "sources": [source],
            "start_line": cursor,
            "limit_per_source": 1,
        },
    )

    assert [row["line"] for row in first["matches"] + second["matches"]] == [1, 3]
    assert first["matches"][0]["spans"] == [{"start_character": 0, "end_character": 6}]
    assert second["source_results"][0]["next_start_line"] is None


async def test_read_does_not_expose_locator_or_trace_document_text() -> None:
    service = _broker_service({"web": FakeBackend("web")})
    service.register_session(make_session())
    source = (await service.call("token", "search.query", {"query": "q"}))[0]["source"]
    row = await service.call(
        "token",
        "content.read",
        {"source": source},
        execution_id="exec-read-no-locator",
    )

    assert "locator" not in row
    assert "locator_error" not in row
    trace = service.take_trace("token", "exec-read-no-locator")[0]
    assert row["text"] not in trace.model_dump_json()


async def test_read_reports_when_one_line_is_truncated() -> None:
    class LongLineBackend(FakeBackend):
        async def fetch(self, hit, *, query=None):
            return DocumentContent(source=hit.source, text="x" * 30 + "\nnext")

    service = _broker_service({"web": LongLineBackend("web")})
    service.register_session(make_session())
    source = (await service.call("token", "search.query", {"query": "q"}))[0]["source"]

    row = await service.call(
        "token",
        "content.read",
        {"source": source, "line_count": 1, "max_chars": 10},
        execution_id="exec-partial-line",
    )

    assert row["text"] == "x" * 10
    assert row["window"]["truncated_by_max_chars"] is True
    assert row["window"]["next"] == {"start_line": 1, "start_character": 10}
    remainder = await service.call(
        "token",
        "content.read",
        {"source": source, **row["window"]["next"], "line_count": 2},
    )
    assert row["text"] + remainder["text"] == "x" * 30 + "\nnext"
    assert remainder["window"]["next"] is None
    assert "locator" not in row


async def test_read_cursor_preserves_a_newline_when_max_chars_ends_at_line_boundary() -> None:
    class BoundaryBackend(FakeBackend):
        async def fetch(self, hit, *, query=None):
            return DocumentContent(source=hit.source, text="abc\ndef")

    service = _broker_service({"web": BoundaryBackend("web")})
    service.register_session(make_session())
    source = (await service.call("token", "search.query", {"query": "q"}))[0]["source"]

    chunks = []
    cursor = {"start_line": 1, "start_character": 0}
    while cursor is not None:
        row = await service.call(
            "token",
            "content.read",
            {"source": source, **cursor, "line_count": 1, "max_chars": 3},
        )
        chunks.append(row["text"])
        cursor = row["window"]["next"]

    assert chunks == ["abc", "\n", "def"]
    assert "".join(chunks) == "abc\ndef"


async def test_grep_returns_context_without_locator() -> None:
    class EvidenceBackend(FakeBackend):
        async def fetch(self, hit, *, query=None):
            return DocumentContent(source=hit.source, text="before\ntarget\nafter")

    service = _broker_service({"web": EvidenceBackend("web")})
    service.register_session(make_session())
    source = (await service.call("token", "search.query", {"query": "q"}))[0]["source"]
    report = await service.call(
        "token",
        "content.grep",
        {"sources": [source], "pattern": "target", "context_lines": 1},
    )
    matches = report["matches"]
    assert matches[0]["before"] == ["before"]
    assert matches[0]["after"] == ["after"]
    assert "locator" not in matches[0]


class CountingBackend:
    """Records how often it was actually asked to retrieve a document."""

    name = "local"
    source_kind = "opaque"

    supports_domains = False

    max_depth = None

    def __init__(self, *, fail: set[str] | None = None) -> None:
        self.fetched: list[str] = []
        self.fail = fail or set()

    async def search(self, query, *, limit, offset=0, domains=None):
        return [
            SearchHit(source="", backend="local", docid=str(index), snippet="s", rank=index)
            for index in range(offset + 1, offset + limit + 1)
        ]

    async def fetch(self, hit, *, query=None):
        self.fetched.append(hit.docid)
        if hit.docid in self.fail:
            raise ProviderRequestError(
                "provider_rejected",
                "Provider rejected one document.",
                retryable=False,
            )
        return DocumentContent(
            source=hit.source,
            text=f"body of {hit.docid}",
            metadata={"docid": hit.docid},
        )

    @staticmethod
    def fetch_candidates(hit: DocumentHandle) -> list[DocumentHandle]:
        return [hit]


async def test_a_document_is_retrieved_once_per_session() -> None:
    """grep and read are meant to be used repeatedly over one pool.

    Without a cache the recommended survey/locate/verify shape refetches every
    candidate once per stage. Against a local index that is merely wasteful;
    against a metered scrape API it is three times the bill and the latency.
    """
    backend = CountingBackend()
    service = _broker_service({"local": backend})
    state = service.register_session(make_session(backends=["local"]))
    hits = await service.call("token", "search.query", {"query": "q", "limit": 3})
    sources = [hit["source"] for hit in hits]

    for source in sources:
        await service.call("token", "content.fetch", {"source": source})
    await service.call("token", "content.grep", {"sources": sources, "pattern": "body"})
    for source in sources:
        await service.call("token", "content.read", {"source": source})

    assert backend.fetched == ["1", "2", "3"]
    # Both numbers are reported: one follows the program's behaviour, the other
    # follows the bill, and a cache is exactly what makes them diverge.
    assert state.policy.usage.content_fetches == 9
    assert state.policy.usage.content_backend_fetches == 3


async def test_callers_can_loop_over_fetch_and_keep_failures_aligned() -> None:
    backend = CountingBackend(fail={"2"})
    service = _broker_service({"local": backend})
    service.register_session(make_session(backends=["local"]))
    hits = await service.call("token", "search.query", {"query": "q", "limit": 3})
    sources = [hit["source"] for hit in hits]

    results: list[tuple[int, dict[str, Any]]] = []
    failures: list[tuple[int, CapabilityProviderError]] = []
    for input_index, source in enumerate(sources):
        try:
            document = await service.call("token", "content.fetch", {"source": source})
        except CapabilityProviderError as exc:
            failures.append((input_index, exc))
        else:
            results.append((input_index, document))

    assert [input_index for input_index, _ in results] == [0, 2]
    assert [row["source"] for _, row in results] == [sources[0], sources[2]]
    assert failures[0][0] == 1
    assert str(failures[0][1]) == "Provider rejected one document."
    assert "HTTPError" not in str(failures[0][1])
    # A failure is not cached: a transient timeout must not be frozen for the
    # rest of the rollout.
    for source in sources:
        with suppress(CapabilityProviderError):
            await service.call("token", "content.fetch", {"source": source})
    assert backend.fetched == ["1", "2", "3", "2"]


async def test_permanent_document_failures_remain_typed_and_sanitized() -> None:
    backend = CountingBackend(fail={"1", "2"})
    service = _broker_service({"local": backend})
    service.register_session(make_session(backends=["local"]))
    hits = await service.call("token", "search.query", {"query": "q", "limit": 2})

    failures: list[CapabilityProviderError] = []
    for index, hit in enumerate(hits):
        with pytest.raises(CapabilityProviderError) as failed:
            await service.call(
                "token",
                "content.fetch",
                {"source": hit["source"]},
                execution_id=f"sanitized-content-{index}",
            )
        failures.append(failed.value)

    assert [failure.code for failure in failures] == [
        "provider_rejected",
        "provider_rejected",
    ]
    assert [str(failure) for failure in failures] == [
        "Provider rejected one document.",
        "Provider rejected one document.",
    ]
    for index in range(2):
        event = service.take_trace("token", f"sanitized-content-{index}")[0]
        assert "HTTPError" not in event.model_dump_json()


async def test_read_is_bounded_by_characters_as_well_as_lines() -> None:
    """A line is a sentence in one corpus and a whole section in another."""

    class Fat(_LocalBackendTraits):
        name = "local"
        supports_domains = False
        max_depth = None

        async def search(self, query, *, limit, offset=0, domains=None):
            return [SearchHit(source="", backend="local", docid="d1", snippet="s", rank=1)]

        async def fetch(self, hit, *, query=None):
            return DocumentContent(source=hit.source, text="\n".join(["x" * 500] * 20))

    service = _broker_service({"local": Fat()})
    service.register_session(make_session(backends=["local"]))
    source = (await service.call("token", "search.query", {"query": "q"}))[0]["source"]

    row = await service.call(
        "token",
        "content.read",
        {"source": source, "line_count": 20, "max_chars": 1200},
    )
    assert len(row["text"]) <= 1200
    assert row["window"]["truncated_by_max_chars"] is True
    assert row["window"]["end_line"] == 3
    assert row["window"]["next"] == {"start_line": 3, "start_character": 198}
    tail = await service.call(
        "token",
        "content.read",
        {"source": source, **row["window"]["next"], "line_count": 20},
    )
    assert row["text"] + tail["text"] == "\n".join(["x" * 500] * 20)


async def test_a_program_can_read_what_it_has_spent() -> None:
    """The compact public view exposes counters and remaining enforced quota."""
    service = _broker_service({"local": FakeBackend("local", depth=5)})
    service.register_session(
        make_session(
            backends=["local"],
            budget=ResourceBudget(max_search_queries=3, max_content_fetches=4),
        )
    )
    await service.call("token", "search.query", {"query": "q", "limit": 2})
    await service.call("token", "content.fetch", {"source": "1"})

    usage = await service.call("token", "session.usage", {})

    assert usage["search_calls"] == 1
    assert usage["content_fetches"] == 1
    assert usage["budget_remaining"]["max_search_queries"] == 2
    assert usage["budget_remaining"]["max_content_fetches"] == 3
    assert set(usage) == {
        "exec_calls",
        "search_calls",
        "content_fetches",
        "llm_calls",
        "pipeline_output_tokens_reserved",
        "sandbox_seconds",
        "workspace_bytes",
        "budget_remaining",
        "terminal_reason",
    }
    state = service.sessions["token"]
    assert state.policy.usage.content_backend_fetches == 1
    assert state.policy.usage.provider_attempts_by_capability == {"content": 1, "search": 1}


def test_canonical_url_folds_only_what_is_safe_to_fold() -> None:
    canonical = canonical_url
    assert canonical("HTTPS://Example.COM/a?utm_source=x&id=7#frag") == (
        "https://example.com/a?id=7"
    )
    # Order of surviving parameters must not decide identity.
    assert canonical("https://e.com/p?b=2&a=1") == canonical("https://e.com/p?a=1&b=2")
    # Paths are left alone: /a and /a/ can be different pages and nothing here
    # can prove otherwise.
    assert canonical("https://e.com/a") != canonical("https://e.com/a/")
    assert canonical("https://e.com/%25C3%25A9") != canonical("https://e.com/%C3%A9")
    assert normalize_web_source("Example.COM/a?utm_source=x&id=7#frag") == (
        "https://example.com/a?id=7"
    )
    assert normalize_web_source("opaque-docid") == "opaque-docid"


async def test_web_content_directly_admits_a_public_url() -> None:
    service = _broker_service({"web": RankedBackend([])})
    state = service.register_session(make_session())
    source = "https://example.com/direct?b=2&a=1#section"

    document = await service.call(
        "token",
        "content.fetch",
        {"source": source},
        execution_id="exec-direct-url",
    )

    assert document["source"] == source
    assert document["text"] == "body"
    record = state.document_for_alias("https://example.com/direct?a=1&b=2")
    assert record is not None
    assert record.admission == "direct_url"
    assert record.fetched is True
    assert state.policy.usage.direct_url_attempts == 1
    assert state.policy.usage.direct_url_successes == 1
    event = service.take_trace("token", "exec-direct-url")[0]
    assert event.hits[0].admission == "direct_url"


async def test_custom_public_url_backend_uses_source_traits_and_document_role() -> None:
    class CustomPublicBackend:
        name = "custom"
        source_kind = "public_url"
        provider_identity = "custom:public"
        supports_domains = False
        max_depth = None

        def __init__(self) -> None:
            self.fetches = 0

        async def search(self, query, *, limit, offset=0, domains=None):
            del query, limit, offset, domains
            return []

        @staticmethod
        def fetch_candidates(hit: DocumentHandle) -> list[DocumentHandle]:
            return [hit]

        async def fetch(self, hit, *, query=None):
            del query
            self.fetches += 1
            return DocumentContent(source=hit.source, text="custom body", url=hit.url)

    backend = CustomPublicBackend()
    service = _broker_service({"custom": backend})
    state = service.register_session(make_session(backends=["custom"]))

    document = await service.call(
        "token",
        "content.fetch",
        {"source": "https://example.com/custom"},
        execution_id="custom-direct",
    )

    assert document["text"] == "custom body"
    assert backend.fetches == 1
    record = state.document_for_alias("https://example.com/custom")
    assert record is not None and record.route == "custom"
    attempts = service.take_trace("token", "custom-direct")[0].provider_attempts
    assert {attempt.component for attempt in attempts} == {"document"}


async def test_custom_opaque_backend_rejects_unsearched_public_url() -> None:
    backend = FakeBackend("custom")
    service = _broker_service({"custom": backend})
    state = service.register_session(make_session(backends=["custom"]))

    with pytest.raises(CapabilityProviderError) as failed:
        await service.call(
            "token",
            "content.fetch",
            {"source": "https://example.com/not-admitted"},
        )

    assert failed.value.code == "url_not_admitted"
    assert failed.value.component == "document"
    assert state.policy.usage.direct_url_attempts == 0


async def test_web_content_directly_admits_a_public_url_without_a_scheme() -> None:
    service = _broker_service({"web": RankedBackend([])})
    state = service.register_session(make_session())
    source = "Example.com/direct?b=2&utm_source=x&a=1#section"

    document = await service.call("token", "content.fetch", {"source": source})

    assert document["source"] == source
    assert document["text"] == "body"
    record = state.document_for_alias("https://example.com/direct?a=1&b=2")
    assert record is not None
    assert record.handle.url == "https://example.com/direct?a=1&b=2"
    assert record.admission == "direct_url"


async def test_schemeless_web_source_matches_a_search_admission() -> None:
    service = _broker_service(
        {"web": RankedBackend(["https://example.com/searched"])},
        content_url_admission="searched_only",
    )
    state = service.register_session(make_session())
    await service.call("token", "search.query", {"query": "q"})

    document = await service.call(
        "token",
        "content.fetch",
        {"source": "example.com/searched"},
        execution_id="exec-schemeless-search-source",
    )

    assert document["source"] == "example.com/searched"
    assert document["text"] == "body"
    assert state.policy.usage.direct_url_attempts == 0
    event = service.take_trace("token", "exec-schemeless-search-source")[0]
    assert event.hits[0].admission == "search"


async def test_strict_content_admission_returns_a_typed_refusal() -> None:
    service = _broker_service(
        {"web": RankedBackend([])},
        content_url_admission="searched_only",
    )
    state = service.register_session(make_session())

    with pytest.raises(CapabilityProviderError) as failed:
        await service.call(
            "token",
            "content.fetch",
            {"source": "https://example.com/not-searched"},
        )

    assert failed.value.code == "url_not_admitted"
    assert failed.value.attempts == 0
    assert state.documents_by_id == {}


@pytest.mark.parametrize("source", ["http://127.0.0.1/private", "127.0.0.1/private"])
async def test_direct_content_rejects_private_urls_before_provider_work(source: str) -> None:
    service = _broker_service({"web": RankedBackend([])})
    state = service.register_session(make_session())

    with pytest.raises(CapabilityProviderError) as failed:
        await service.call("token", "content.fetch", {"source": source})

    assert failed.value.code == "unknown_source"
    assert failed.value.attempts == 0
    assert state.policy.usage.content_backend_fetches == 0
    assert state.documents_by_id == {}


async def test_trace_records_identity_and_rank_for_every_hit() -> None:
    """Rank and duplication cannot be recovered after the fact.

    A baseline that logged only `result_count` can never be asked afterwards
    whether ranking or duplicate candidates were the bottleneck -- which is
    exactly the question that decides whether a fusion/dedup layer is worth
    building.
    """
    service = _broker_service(
        {"web": RankedBackend(["https://example.com/a", "https://example.com/b"])}
    )
    service.register_session(make_session())

    await service.call(
        "token",
        "search.query_many",
        {"queries": ["one", "two"], "limit": 2},
        execution_id="exec-hits",
    )
    event = service.take_trace("token", "exec-hits")[0]

    # A fan-out lands in one event, so per-query duplication is visible in it.
    assert len(event.hits) == 4
    assert [hit.rank for hit in event.hits] == [1, 2, 1, 2]
    assert len({hit.identity for hit in event.hits}) == 2
    assert event.hits[0].score == 1.0
    assert {hit.admission for hit in event.hits} == {"search"}
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
    service = _broker_service({"local": FakeBackend("local", depth=3)})
    service.register_session(make_session(backends=["local"]))
    await service.call("token", "search.query", {"query": "q", "limit": 3})

    await service.call(
        "token",
        "content.grep",
        {"sources": ["2", "3"], "pattern": "body"},
        execution_id="exec-read",
    )
    event = service.take_trace("token", "exec-read")[0]

    assert [hit.identity for hit in event.hits] == ["local:docid:2", "local:docid:3"]
    # The rank of the sighting that put the document in reach, not of this call.
    assert [hit.rank for hit in event.hits] == [2, 3]
    assert {hit.admission for hit in event.hits} == {"search"}
    assert "content:" not in event.model_dump_json()


async def test_unknown_source_and_invalid_pattern_are_top_level_errors() -> None:
    service = _broker_service({"local": FakeBackend("local", depth=2)})
    service.register_session(make_session(backends=["local"]))
    await service.call("token", "search.query", {"query": "q", "limit": 2})

    with pytest.raises(CapabilityProviderError) as failed:
        await service.call(
            "token",
            "content.fetch",
            {"source": "nope"},
            execution_id="exec-unknown",
        )
    with pytest.raises(ValueError):
        await service.call(
            "token",
            "content.grep",
            {"sources": ["1"], "pattern": ""},
            execution_id="exec-pattern",
        )

    unknown = service.take_trace("token", "exec-unknown")[0]
    pattern = service.take_trace("token", "exec-pattern")[0]

    assert failed.value.code == "unknown_source"
    assert unknown.status == "error"
    assert unknown.error_type == "CapabilityProviderError"
    assert pattern.error_type == "ValueError"
    assert "pattern must not be empty" in (pattern.error or "")
    assert [hit.identity for hit in unknown.hits] == ["local:docid:nope"]


def test_a_trace_error_message_is_bounded_but_never_empty() -> None:
    """A backend is free to put a response body in an exception.

    Bounded for volume rather than secrecy -- the trace already records
    addresses and queries verbatim -- and `None` rather than `""` for a bare
    raise, because an empty string reads as "the message was dropped".
    """
    long = trace_error_message(RuntimeError("x" * 500))

    assert long is not None
    assert long.startswith("x" * 32) and long.endswith("... [truncated]")
    assert len(long) < 500
    assert trace_error_message(ValueError()) is None


async def test_batching_disabled_forces_one_item_per_call() -> None:
    """The switch bounds the only remaining public fan-out operation."""
    service = _broker_service({"web": RankedBackend(["https://example.com/a"])})
    service.register_session(make_session(mechanisms=Mechanisms(batching=False)))

    with pytest.raises(MechanismDisabled, match="at most one item"):
        await service.call(
            "token",
            "search.query_many",
            {"queries": ["one", "two"]},
            execution_id="exec-block",
        )
    report = await service.call("token", "search.query_many", {"queries": ["one"]})
    assert len(report["results"][0]["hits"]) == 1

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
    service = _broker_service({"web": RankedBackend(["https://example.com/a"])})
    service.register_session(make_session(mechanisms=Mechanisms(context_decoupling=False)))

    hits = await service.call("token", "search.query", {"query": "q"}, execution_id="exec-echo")
    event = service.take_trace("token", "exec-echo")[0]
    assert event.result_payload == hits
    assert event.result_payload_truncated is False


async def test_default_sessions_keep_results_out_of_the_trace() -> None:
    service = _broker_service({"web": RankedBackend(["https://example.com/a"])})
    service.register_session(make_session())
    await service.call("token", "search.query", {"query": "q"}, execution_id="exec-plain")
    event = service.take_trace("token", "exec-plain")[0]
    assert event.result_payload is None


async def test_oversized_payload_is_capped_and_says_so() -> None:
    service = _broker_service(
        {"web": RankedBackend([f"https://example.com/{index}" for index in range(50)])},
        max_context_payload_bytes=200,
    )
    service.register_session(make_session(mechanisms=Mechanisms(context_decoupling=False)))
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
    service = _broker_service({"web": RankedBackend(["https://example.com/a"])})
    service.register_session(make_session())
    with pytest.raises(ValueError, match="Unsupported capability"):
        await service.call("token", "search.nope", {})
    for removed in (
        "content.get_many",
        "content.read_many",
        "llm.complete_many",
        "llm.extract_many",
    ):
        with pytest.raises(ValueError, match="Unsupported capability"):
            await service.call("token", removed, {})


def test_capabilities_manifest_drops_only_what_is_disabled() -> None:
    assert CAPABILITY_METHODS == BROKER_METHODS
    assert Mechanisms().capabilities() == list(CAPABILITY_METHODS)
    assert "content.snippets" not in CAPABILITY_METHODS
    assert "content.grep" in CAPABILITY_METHODS
    assert "content.grep_report" not in CAPABILITY_METHODS
    without_llm = Mechanisms(llm_subroutine=False).capabilities()
    assert not any(method.startswith("llm.") for method in without_llm)
    assert "search.query_many" in without_llm
    # Batching now controls only search.query_many; the method stays advertised
    # and rejects fan-out wider than one at runtime when disabled.
    assert Mechanisms(batching=False).capabilities() == list(CAPABILITY_METHODS)


async def test_session_capabilities_reflect_backend_limits_and_mechanisms() -> None:
    service = _broker_service(
        {"local": FakeBackend("local", depth=5)},
        max_search_queries_per_request=7,
        max_search_query_chars=123,
        max_search_top_k=50,
        max_content_sources_per_request=9,
        content_url_admission="searched_only",
    )
    service.register_session(
        make_session(
            backends=["local"],
            mechanisms=Mechanisms(batching=False, persistence=False),
        )
    )

    capabilities = await service.call("token", "session.capabilities", {})

    assert capabilities["contracts"] == {"sandbox": 14, "capability": 13}
    assert capabilities["search"] == {
        "backend": "local",
        "supports_include_domains": False,
        "max_depth": None,
        "limits": {
            "max_queries_per_request": 7,
            "max_query_chars": 123,
            "max_top_k": 50,
            "max_limit": 100,
            "max_offset": 500,
            "max_concurrency": 20,
        },
    }
    assert capabilities["content"]["url_admission"] == "searched_only"
    assert capabilities["content"]["limits"]["max_sources_per_request"] == 9
    assert capabilities["llm"]["available"] is False
    assert capabilities["mechanisms"]["batching"] is False
    assert capabilities["mechanisms"]["persistence"] is False
    assert "model" not in json.dumps(capabilities).lower()


async def test_equivalent_url_spelling_resolves_the_admitted_source() -> None:
    service = _broker_service(
        {"web": RankedBackend(["https://Example.com/a?utm_source=x&id=7#fragment"])}
    )
    service.register_session(make_session())
    source = (await service.call("token", "search.query", {"query": "q"}))[0]["source"]

    alternate = "HTTPS://EXAMPLE.COM/a?id=7#other"
    alternate_document = await service.call("token", "content.fetch", {"source": alternate})
    source_document = await service.call("token", "content.fetch", {"source": source})
    assert alternate_document["source"] == alternate
    assert source_document["source"] == source
    assert alternate_document["text"] == source_document["text"]


async def test_a_mistyped_source_is_refused_rather_than_repaired() -> None:
    service = _broker_service({"local": FakeBackend("local")})
    service.register_session(make_session(backends=["local"]))
    hits = await service.call("token", "search.query", {"query": "q"})
    source = hits[0]["source"]

    one_character_off = source[:-1] + ("0" if source[-1] != "0" else "1")
    with pytest.raises(CapabilityProviderError) as failed:
        await service.call("token", "content.fetch", {"source": one_character_off})
    assert failed.value.code == "unknown_source"
    # Prefix/suffix mistakes are not repaired into an admitted source.
    with pytest.raises(CapabilityProviderError) as failed:
        await service.call("token", "content.fetch", {"source": source + "x"})
    assert failed.value.code == "unknown_source"


async def test_fetch_requires_the_singular_source_parameter() -> None:
    service = _broker_service({"local": FakeBackend("local")})
    service.register_session(make_session(backends=["local"]))
    hits = await service.call("token", "search.query", {"query": "q"})

    document = await service.call("token", "content.fetch", {"source": hits[0]["source"]})
    assert document["source"] == hits[0]["source"]
    with pytest.raises(ValueError, match="accepts one source"):
        await service.call("token", "content.fetch", {"sources": [hits[0]["source"]]})


async def test_a_snippet_carries_the_date_of_the_hit_that_found_it() -> None:
    """Filled from the hit, so a backend cannot forget to."""
    service = _broker_service({"local": FakeBackend("local")})
    service.register_session(make_session(backends=["local"]))
    hits = await service.call("token", "search.query", {"query": "q"})
    document = await service.call("token", "content.fetch", {"source": hits[0]["source"]})
    assert document.get("date") == hits[0].get("date")


async def _wait_for_condition(predicate, *, turns: int = 200) -> None:
    for _ in range(turns):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition did not become true")


class _CoalescingBatchBackend:
    name = "local"
    source_kind = "opaque"
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
                    source="",
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
        return DocumentContent(source=hit.source, text=f"body:{hit.docid}")

    @staticmethod
    def fetch_candidates(hit: DocumentHandle) -> list[DocumentHandle]:
        return [hit]


class _CoalescingSearchBackend:
    name = "web"
    source_kind = "public_url"
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
                source="",
                backend="web",
                title=query,
                url=f"https://example.com/{query}",
                snippet="snippet",
                rank=1,
                metadata={"nested": {"value": query}},
            )
        ]

    async def fetch(self, hit, *, query=None):
        return DocumentContent(source=hit.source, text="body", url=hit.url)

    @staticmethod
    def fetch_candidates(hit: DocumentHandle) -> list[DocumentHandle]:
        return [hit]


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
        return DocumentContent(
            source=hit.source,
            text="shared full document",
            url=hit.url,
            metadata={"nested": {"value": 1}},
        )


async def test_inflight_coalescing_overlapping_local_batches_and_duplicates() -> None:
    backend = _CoalescingBatchBackend()
    service = _broker_service({"local": backend}, provider_execution_config=COALESCING)
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
    first_report, second_report = await asyncio.gather(first, second)

    first_results = first_report["results"]
    second_results = second_report["results"]
    assert [row["query"] for row in first_results] == ["alpha", "beta"]
    assert [row["query"] for row in second_results] == ["beta", "beta", "gamma"]
    assert second_results[0] is not second_results[1]
    second_results[0]["hits"][0]["title"] = "changed"
    assert second_results[1]["hits"][0]["title"] == "beta"
    assert state.flights == {}
    assert state.policy.usage.provider_attempts_by_capability["search"] == 2
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
    service = _broker_service(
        {"local": backend},
        provider_execution_config=ProviderExecutionConfig(
            inflight_coalescing=True,
            max_inflight_keys=1,
        ),
    )
    state = service.register_session(make_session(backends=["local"]))
    leader = asyncio.create_task(service.call("token", "search.query", {"query": "alpha"}))
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
    service = _broker_service(
        {"local": backend},
        provider_execution_config=ProviderExecutionConfig(
            inflight_coalescing=True,
            max_waiters_per_flight=2,
        ),
    )
    state = service.register_session(make_session(backends=["local"]))
    leader = asyncio.create_task(service.call("token", "search.query", {"query": "same"}))
    await _wait_for_condition(lambda: len(backend.calls) == 1)
    duplicate_follower = asyncio.create_task(
        service.call(
            "token",
            "search.query_many",
            {"queries": ["same", "same"]},
        )
    )
    await _wait_for_condition(lambda: next(iter(state.flights.values())).waiters == 2)

    with pytest.raises(InflightCapacityError):
        await service.call("token", "search.query", {"query": "same"})
    assert backend.calls == [["same"]]
    assert next(iter(state.flights.values())).waiters == 2

    backend.release.set()
    leader_rows, follower_report = await asyncio.gather(leader, duplicate_follower)
    assert leader_rows[0]["title"] == "same"
    assert len(follower_report["results"]) == 2


async def test_inflight_feature_disabled_keeps_independent_transports() -> None:
    backend = _CoalescingSearchBackend()
    service = _broker_service({"web": backend})
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
    assert state.policy.usage.provider_attempts_by_capability["search"] == 2


async def test_cancelling_leader_detaches_without_cancelling_follower() -> None:
    backend = _CoalescingSearchBackend()
    service = _broker_service({"web": backend}, provider_execution_config=COALESCING)
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
        lambda: len(state.flights) == 1 and next(iter(state.flights.values())).waiters == 2
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
    assert state.policy.usage.provider_attempts_by_capability["search"] == 1


async def test_cancel_execution_drains_last_waiter_group_and_trace() -> None:
    backend = _CoalescingSearchBackend()
    service = _broker_service({"web": backend}, provider_execution_config=COALESCING)
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
    service = _broker_service({"web": backend}, provider_execution_config=COALESCING)
    state = service.register_session(make_session())
    call = asyncio.create_task(service.call("token", "search.query", {"query": "race"}))
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
    service = _broker_service({"web": backend}, provider_execution_config=COALESCING)
    state = service.register_session(make_session())
    hits = await service.call("token", "search.query", {"query": "doc"})
    source = hits[0]["source"]

    first = asyncio.create_task(
        service.call(
            "token",
            "content.fetch",
            {"source": source},
            execution_id="content-leader",
        )
    )
    await backend.fetch_started.wait()
    second = asyncio.create_task(
        service.call(
            "token",
            "content.fetch",
            {"source": source},
            execution_id="content-follower",
        )
    )
    await _wait_for_condition(
        lambda: len(state.flights) == 1 and next(iter(state.flights.values())).waiters == 2
    )
    backend.fetch_release.set()
    first_document, second_document = await asyncio.gather(first, second)

    assert backend.fetch_calls == 1
    assert state.policy.usage.content_fetches == 2
    assert state.policy.usage.content_backend_fetches == 1
    assert state.policy.usage.provider_coalesced_requests == 1
    first_document["metadata"]["nested"]["value"] = 9
    assert second_document["metadata"]["nested"]["value"] == 1
    leader_trace = service.take_trace("token", "content-leader")
    follower_trace = service.take_trace("token", "content-follower")
    assert len(leader_trace[0].provider_attempts) == 1
    assert follower_trace[0].provider_attempts == []
    assert len(follower_trace[0].coalesced_requests) == 1


async def test_content_leader_caches_before_flight_cleanup_lock_queue() -> None:
    backend = _CoalescingContentBackend()
    service = _broker_service({"web": backend}, provider_execution_config=COALESCING)
    state = service.register_session(make_session())
    source = (await service.call("token", "search.query", {"query": "doc"}))[0]["source"]
    record = state.document_for_alias(source)
    assert record is not None
    identity = record.document_id
    leader = asyncio.create_task(service.call("token", "content.fetch", {"source": source}))
    await backend.fetch_started.wait()

    # Hold the registry lock so the completed transport's cleanup is queued.
    # The cache must already be visible while the old flight is still present;
    # otherwise a third caller also queues on this lock and later starts a
    # duplicate fetch in the flight/cache publication gap.
    await state.flight_lock.acquire()
    try:
        backend.fetch_release.set()
        await _wait_for_condition(lambda: identity in state.content_cache)
        assert len(state.flights) == 1

        third = asyncio.create_task(service.call("token", "content.fetch", {"source": source}))
        await _wait_for_condition(third.done)
        assert (await third)["text"] == "shared full document"
        assert backend.fetch_calls == 1
    finally:
        state.flight_lock.release()

    assert (await leader)["text"] == "shared full document"
    assert state.flights == {}


async def test_content_refreshes_stale_misses_after_usage_reservation() -> None:
    backend = _CoalescingContentBackend()
    service = _broker_service({"web": backend}, provider_execution_config=COALESCING)
    state = service.register_session(make_session())
    source = (await service.call("token", "search.query", {"query": "doc"}))[0]["source"]
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
    leader = asyncio.create_task(service.call("token", "content.fetch", {"source": source}))
    await backend.fetch_started.wait()
    follower = asyncio.create_task(service.call("token", "content.fetch", {"source": source}))
    await follower_reserved.wait()

    backend.fetch_release.set()
    assert (await leader)["text"] == "shared full document"
    resume_follower.set()
    assert (await follower)["text"] == "shared full document"
    assert backend.fetch_calls == 1
    assert state.policy.usage.content_fetches == 2
    assert state.policy.usage.content_backend_fetches == 1


async def test_content_rechecks_cache_after_waiting_for_flight_admission() -> None:
    backend = _CoalescingContentBackend()
    service = _broker_service({"web": backend}, provider_execution_config=COALESCING)
    state = service.register_session(make_session())
    source = (await service.call("token", "search.query", {"query": "doc"}))[0]["source"]
    leader = asyncio.create_task(service.call("token", "content.fetch", {"source": source}))
    await backend.fetch_started.wait()

    original_admit = service.providers.flights.admit
    follower_at_admission = asyncio.Event()
    resume_follower = asyncio.Event()

    async def gated_admit(state_arg, requests, *, group_new):
        follower_at_admission.set()
        await resume_follower.wait()
        return await original_admit(state_arg, requests, group_new=group_new)

    service.providers.flights.admit = gated_admit
    follower = asyncio.create_task(service.call("token", "content.fetch", {"source": source}))
    await follower_at_admission.wait()

    # The follower already classified the row as a miss, but has not acquired
    # the flight registry lock. Let the old leader cache and remove its flight
    # before the follower admits a new key.
    backend.fetch_release.set()
    assert (await leader)["text"] == "shared full document"
    assert state.flights == {}
    resume_follower.set()

    assert (await follower)["text"] == "shared full document"
    assert backend.fetch_calls == 1
    assert state.policy.usage.content_backend_fetches == 1


async def test_local_partial_batch_attempt_is_sanitized_and_traced_as_partial() -> None:
    class PartialBackend(_CoalescingBatchBackend):
        async def search_many(self, queries, *, limit, offset=0, domains=None):
            return [
                self._batch(queries[0]),
                SearchBatchFailure(
                    code="provider_rejected",
                    message="secret provider response body",
                    retryable=False,
                ),
            ]

    backend = PartialBackend()
    service = _broker_service({"local": backend})
    service.register_session(make_session(backends=["local"]))

    report = await service.call(
        "token",
        "search.query_many",
        {"queries": ["ok", "bad"]},
        execution_id="partial-batch",
    )

    assert len(report["results"][0]["hits"]) == 1
    assert report["failures"][0]["message"] == "Provider rejected one search item."
    assert "secret" not in json.dumps(report)
    trace = service.take_trace("token", "partial-batch")[0]
    assert trace.provider_attempts[0].status == "partial"
    assert "secret" not in trace.model_dump_json()
