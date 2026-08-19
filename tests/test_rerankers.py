from __future__ import annotations

import json

import httpx
import pytest

from opensac._contracts import ContentSnippet, SearchHit
from opensac.backends.rerank.jina import JinaPassageReranker
from opensac.broker.service import BrokerService
from opensac.models import ResourceBudget, Session
from opensac.provider import ProviderPolicy, ProviderRequestError, ProviderRuntime


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
        return ContentSnippet(
            source=hit.source,
            title=hit.title,
            url=hit.url,
            text=f"rankable private passage from {hit.title}",
        )


def _mocked_reranker(
    handler,
    *,
    api_key: str = "jina-secret",
    model: str = "jina-reranker-v3",
) -> JinaPassageReranker:
    reranker = JinaPassageReranker(api_key=api_key, model=model)
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
    reranker = JinaPassageReranker(api_key=api_key, model=model)

    with pytest.raises(ProviderRequestError) as raised:
        reranker.preflight()

    assert raised.value.code == "provider_not_configured"
    assert raised.value.attempts == 0
    assert expected in str(raised.value)
    assert reranker._client is None


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
        {
            "web.rerank": ProviderPolicy(
                retry_profile="safe",
                max_attempts=3,
                base_backoff_seconds=0,
                max_backoff_seconds=0,
            )
        }
    )
    service = BrokerService(
        {"web": _TwoPageBackend()},
        passage_reranker=reranker,
        provider_runtime=runtime,
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
        attempt for attempt in trace.provider_attempts if attempt.operation == "web.rerank"
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
    service = BrokerService(
        {"web": _TwoPageBackend()},
        passage_reranker=reranker,
    )
    service.register_session(_session())
    try:
        source = (await service.call("token", "search.query", {"query": "seed"}))[0]["source"]
        with pytest.raises(ProviderRequestError) as raised:
            await service.call(
                "token",
                "content.passages",
                {"query": "rankable", "sources": [source]},
                execution_id="jina-final-error",
            )
        trace = service.take_trace("token", "jina-final-error")[0]
    finally:
        await service.aclose()

    assert raised.value.code == "provider_unavailable"
    assert raised.value.attempts == 1
    assert secret_body not in str(raised.value)
    assert secret_body not in trace.model_dump_json()
