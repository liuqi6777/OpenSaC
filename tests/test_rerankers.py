from __future__ import annotations

import json

import httpx
import pytest

from opensac.backends.document import DocumentContent, DocumentHandle
from opensac.backends.rerank import JinaReranker, LexicalReranker, RerankScore
from opensac.backends.search import SearchHit
from opensac.broker.call_context import call_scope
from opensac.broker.service import BrokerService
from opensac.broker.services import RerankItem
from opensac.models import ResourceBudget, Session
from opensac.provider import ProviderPolicy, ProviderRequestError, ProviderRuntime


def _broker_service(search_backends, *, document_backends=None, **kwargs):
    if document_backends is None:
        document_backends = search_backends
    return BrokerService(
        search_backends,
        document_backends=document_backends,
        **kwargs,
    )


def _session() -> Session:
    return Session(
        id="sess-reranker",
        token="token",
        backends=["web"],
        workspace="/tmp/session-reranker",
        budget=ResourceBudget(),
    )


class _TwoPageBackend:
    name = "web"
    source_kind = "public_url"
    supports_domains = True
    max_depth = 100

    async def search(self, query, *, limit, offset=0, domains=None):
        del query, domains
        return [
            SearchHit(
                source="",
                backend="web",
                title=f"Page {index}",
                url=f"https://example.test/{index}",
                snippet="preview",
                rank=index + 1,
            )
            for index in range(offset, min(offset + limit, 2))
        ]

    async def fetch(self, hit, *, query=None):
        del query
        return DocumentContent(
            source=hit.source,
            title=hit.title,
            url=hit.url,
            text=f"rankable private passage from {hit.title}",
        )

    @staticmethod
    def fetch_candidates(hit: DocumentHandle) -> list[DocumentHandle]:
        return [hit]


def _mocked_reranker(
    handler,
    *,
    api_key: str = "jina-secret",
    model: str = "jina-reranker-v3",
) -> JinaReranker:
    reranker = JinaReranker(api_key=api_key, model=model)
    reranker._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return reranker


async def test_jina_adapter_sends_indexed_documents_and_accepts_unordered_results() -> None:
    seen: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer jina-secret"
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": 2, "relevance_score": 0.7},
                    {"index": 0, "relevance_score": 0.9},
                    {"index": 1, "relevance_score": 0.8},
                ]
            },
        )

    reranker = _mocked_reranker(handler)
    try:
        results = await reranker.rerank("evidence query", ["zero", "one", "two"])
    finally:
        await reranker.aclose()

    assert seen == [
        {
            "model": "jina-reranker-v3",
            "query": "evidence query",
            "documents": ["zero", "one", "two"],
            "top_n": 3,
            "return_documents": False,
        }
    ]
    assert [(row.index, row.score) for row in results] == [
        (2, 0.7),
        (0, 0.9),
        (1, 0.8),
    ]


async def test_lexical_reranker_implements_the_generic_indexed_contract() -> None:
    reranker = LexicalReranker()

    reranker.preflight()
    results = await reranker.rerank(
        "Singapore revenue",
        [
            "The audited report states that Singapore revenue increased.",
            "An unrelated document about weather.",
        ],
    )

    assert reranker.name == "lexical:bm25"
    assert reranker.provider_identity == "lexical:bm25:v1"
    assert [result.index for result in results] == [0, 1]
    assert results[0].score > results[1].score == 0.0


@pytest.mark.parametrize(
    "results",
    [
        [{"index": 0, "relevance_score": 1.0}],
        [
            {"index": 0, "relevance_score": 1.0},
            {"index": 0, "relevance_score": 0.5},
        ],
        [
            {"index": 0, "relevance_score": 1.0},
            {"index": 2, "relevance_score": 0.5},
        ],
        [
            {"index": 0, "relevance_score": 1.0},
            {"index": 1, "relevance_score": "secret invalid score"},
        ],
    ],
)
async def test_jina_adapter_rejects_incomplete_duplicate_or_invalid_indexes(results) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": results})

    reranker = _mocked_reranker(handler)
    try:
        with pytest.raises(ProviderRequestError) as raised:
            await reranker.rerank("query", ["zero", "one"])
    finally:
        await reranker.aclose()

    assert raised.value.code == "provider_invalid_response"
    assert "secret invalid score" not in str(raised.value)


@pytest.mark.parametrize(
    ("api_key", "model", "expected"),
    [
        ("", "jina-reranker-v3", "API key"),
        ("jina-secret", "", "model"),
        ("   ", "   ", "API key and model"),
        ("", "", "API key and model"),
    ],
)
def test_jina_adapter_preflight_requires_credentials_and_explicit_model(
    api_key: str,
    model: str,
    expected: str,
) -> None:
    reranker = JinaReranker(api_key=api_key, model=model)

    with pytest.raises(ProviderRequestError) as raised:
        reranker.preflight()

    assert raised.value.code == "provider_not_configured"
    assert raised.value.attempts == 0
    assert expected in str(raised.value)
    assert reranker._client is None


def test_broker_defaults_to_lexical_reranker_and_accepts_its_runtime() -> None:
    runtime = ProviderRuntime()
    broker = _broker_service(
        {"web": _TwoPageBackend()},
        rerank_runtime=runtime,
    )

    assert isinstance(broker.reranker, LexicalReranker)
    assert broker.rerank_runtime is runtime
    assert broker.rerank_service.backend is broker.reranker


async def test_shared_rerank_service_attributes_usage_to_the_calling_capability() -> None:
    class GenericReranker:
        name = "test:generic"
        provider_identity = "test:generic"

        @staticmethod
        def preflight() -> None:
            return None

        async def rerank(self, query: str, documents: list[str]) -> list[RerankScore]:
            del query
            return [
                RerankScore(index=index, score=float(len(document)))
                for index, document in enumerate(documents)
            ]

    runtime = ProviderRuntime(ProviderPolicy(concurrency=1))
    broker = _broker_service(
        {"web": _TwoPageBackend()},
        reranker=GenericReranker(),
        rerank_runtime=runtime,
    )
    state = broker.register_session(_session())
    service = broker.rerank_service

    with call_scope("token", None, capability_family="search") as search_context:
        search_scores = await service.score(
            state,
            "query",
            [RerankItem(id="hit", text="search result")],
        )
    with call_scope("token", None, capability_family="content"):
        passage_scores = await service.score(
            state,
            "query",
            [RerankItem(id="passage", text="content passage")],
        )

    assert search_scores == {"hit": 13.0}
    assert passage_scores == {"passage": 15.0}
    assert state.policy.usage.provider_attempts_by_capability == {
        "content": 1,
        "search": 1,
    }
    assert {attempt.component for attempt in search_context.provider_attempts} == {"rerank"}
    assert len(runtime._governors) == 1


async def test_rerank_service_rejects_invalid_backend_output() -> None:
    class InvalidReranker:
        name = "test:invalid"
        provider_identity = "test:invalid"

        @staticmethod
        def preflight() -> None:
            return None

        async def rerank(self, query: str, documents: list[str]):
            del query, documents
            return [{"index": True, "score": 1.0}]

    broker = _broker_service(
        {"web": _TwoPageBackend()},
        reranker=InvalidReranker(),
    )
    state = broker.register_session(_session())

    with (
        call_scope("token", None, capability_family="content"),
        pytest.raises(ProviderRequestError) as failed,
    ):
        await broker.rerank_service.score(
            state,
            "query",
            [RerankItem(id="passage", text="content passage")],
        )

    assert failed.value.code == "provider_invalid_response"
    assert failed.value.attempts == 1


async def test_jina_reranking_uses_provider_retries_and_body_free_trace() -> None:
    calls = 0
    secret_body = "SECRET JINA RESPONSE BODY"

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, request=request, text=secret_body)
        if calls == 2:
            return httpx.Response(503, request=request, text=secret_body)
        return httpx.Response(
            200,
            request=request,
            json={
                "results": [
                    {"index": 1, "relevance_score": 0.9},
                    {"index": 0, "relevance_score": 0.4},
                ]
            },
        )

    reranker = _mocked_reranker(handler)
    runtime = ProviderRuntime(
        ProviderPolicy(
            retry_profile="safe",
            max_attempts=3,
            base_backoff_seconds=0,
            max_backoff_seconds=0,
        )
    )
    service = _broker_service(
        {"web": _TwoPageBackend()},
        reranker=reranker,
        rerank_runtime=runtime,
    )
    service.register_session(_session())
    try:
        hits = await service.call("token", "search.query", {"query": "seed", "limit": 2})
        report = await service.call(
            "token",
            "content.passages",
            {
                "query": "rankable passage",
                "sources": [hit["source"] for hit in hits],
                "limit": 2,
                "max_per_source": 1,
            },
            execution_id="jina-retry",
        )
        trace = service.take_trace("token", "jina-retry")[0]
    finally:
        await service.aclose()

    assert calls == 3
    assert [row["source"] for row in report["passages"]] == [hits[1]["source"], hits[0]["source"]]
    rerank_attempts = [
        attempt for attempt in trace.provider_attempts if attempt.component == "rerank"
    ]
    assert [attempt.status for attempt in rerank_attempts] == [
        "error",
        "error",
        "success",
    ]
    assert [attempt.error_code for attempt in rerank_attempts] == [
        "provider_rate_limited",
        "provider_unavailable",
        None,
    ]
    serialized = trace.model_dump_json()
    assert secret_body not in serialized
    assert "rankable private passage" not in serialized


async def test_jina_final_http_error_is_typed_and_does_not_expose_response_body() -> None:
    secret_body = "SECRET FINAL JINA BODY"

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, request=request, text=secret_body)

    reranker = _mocked_reranker(handler)
    service = _broker_service(
        {"web": _TwoPageBackend()},
        reranker=reranker,
    )
    service.register_session(_session())
    try:
        source = (await service.call("token", "search.query", {"query": "seed"}))[0]["source"]
        report = await service.call(
            "token",
            "content.passages",
            {"query": "rankable", "sources": [source]},
            execution_id="jina-final-error",
        )
        trace = service.take_trace("token", "jina-final-error")[0]
    finally:
        await service.aclose()

    assert report["passages"][0]["ranker"] == "lexical:bm25"
    assert report["warnings"][0]["code"] == "provider_unavailable"
    assert report["warnings"][0]["attempts"] == 1
    assert secret_body not in json.dumps(report)
    assert secret_body not in trace.model_dump_json()
