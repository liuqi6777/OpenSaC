from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace

import httpx
import pytest

from opensac.backends.document import DocumentContent, DocumentHandle, document_fetch_candidates
from opensac.backends.document.local_http import LocalDocumentBackend
from opensac.backends.search import SearchHit
from opensac.backends.search.local_http import LocalSearchBackend
from opensac.backends.search.serper import SerperBackend
from opensac.broker.capabilities.content import ContentLimits
from opensac.broker.config import BrokerConfig
from opensac.broker.providers import ProviderExecutionConfig, ProviderExecutor
from opensac.broker.providers.cache import ProviderResultCache
from opensac.broker.providers.execution import CapabilityProviderError
from opensac.broker.providers.serialization import canonical_json_bytes
from opensac.broker.service import BrokerService, RetrievalRoute
from opensac.broker.sources import document_identity
from opensac.models import Mechanisms, ResourceBudget, Session
from opensac.provider import ProviderPolicy, ProviderRequestError, ProviderRuntime


def _broker_service(
    search_backends,
    *,
    document_backends=None,
    backend_revision="",
    **kwargs,
):
    if document_backends is None:
        document_backends = search_backends
    routes = {
        name: RetrievalRoute(
            search=backend,
            document=document_backends[name],
            revision=backend_revision,
        )
        for name, backend in search_backends.items()
    }
    return BrokerService(routes, **kwargs)


def make_session(
    *,
    backend: str = "local",
    session_id: str = "sess_provider",
    token: str = "token",
) -> Session:
    return Session(
        id=session_id,
        token=token,
        backends=[backend],
        workspace="/tmp/provider-session",
        mechanisms=Mechanisms(),
        budget=ResourceBudget(),
    )


class LocalBackend:
    name = "local"
    source_kind = "opaque"
    supports_domains = False
    max_depth = None
    provider_identity = "local:test"

    def __init__(self, *, documents: dict[str, str] | None = None) -> None:
        self.fetches: list[str] = []
        self.documents = documents or {"1": "body"}
        self.failures: dict[str, ProviderRequestError] = {}

    @staticmethod
    def _hit(docid: str, rank: int) -> SearchHit:
        return SearchHit(
            source="",
            backend="local",
            docid=docid,
            title=f"document {docid}",
            snippet=f"preview {docid}",
            rank=rank,
        )

    async def search(self, query, *, limit, offset=0, domains=None):
        del query, domains
        docids = list(self.documents)[offset : offset + limit]
        return [self._hit(docid, offset + rank) for rank, docid in enumerate(docids, 1)]

    async def fetch(self, hit, *, query=None):
        del query
        docid = str(hit.docid)
        self.fetches.append(docid)
        if failure := self.failures.get(docid):
            raise failure
        return DocumentContent(
            source=hit.source,
            text=self.documents[docid],
            title=hit.title,
            metadata={"backend": "local", "docid": docid},
        )

    @staticmethod
    def fetch_candidates(hit: DocumentHandle) -> list[DocumentHandle]:
        return [hit]


class _WebBackendTraits:
    name = "web"
    source_kind = "public_url"
    result_cacheable = True
    supports_domains = True
    max_depth = 100

    @staticmethod
    def fetch_candidates(hit: DocumentHandle) -> list[DocumentHandle]:
        return [hit]


class WebCacheBackend(_WebBackendTraits):
    provider_identity = "web:cache-test"

    def __init__(self) -> None:
        self.search_calls = 0
        self.fetch_calls = 0
        self.failures_remaining = 0
        self.block_first = False
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def search(self, query, *, limit, offset=0, domains=None):
        del domains
        self.search_calls += 1
        if self.block_first and self.search_calls == 1:
            self.started.set()
            await self.release.wait()
        if self.failures_remaining:
            self.failures_remaining -= 1
            raise ProviderRequestError(
                "provider_unavailable",
                "Provider is temporarily unavailable.",
                retryable=True,
            )
        return [
            SearchHit(
                backend="web",
                url=f"https://example.com/{query}",
                title=query,
                rank=offset + 1,
            )
        ][:limit]

    async def fetch(self, hit, *, query=None):
        del query
        self.fetch_calls += 1
        return DocumentContent(
            source=hit.source,
            text=f"body for {hit.source}",
            url=hit.url,
            title=hit.title,
        )


async def test_broker_runs_bundled_search_preflight_before_provider_usage() -> None:
    backend = SerperBackend("")
    runtime = ProviderRuntime(
        ProviderPolicy(
            requests_per_second=1.0,
            burst=1,
        )
    )
    service = _broker_service(
        {"web": backend},
        document_backends={"web": WebCacheBackend()},
        search_runtime=runtime,
    )
    state = service.register_session(make_session(backend="web"))

    with pytest.raises(ProviderRequestError) as failed:
        await service.call(
            "token",
            "search.query",
            {"query": "alpha"},
            execution_id="search-preflight",
        )

    assert failed.value.code == "provider_not_configured"
    assert failed.value.attempts == 0
    assert state.policy.usage.search_calls == 1
    assert state.policy.usage.provider_attempts_by_capability.get("search", 0) == 0
    assert state.policy.usage.provider_retries == 0
    assert state.policy.usage.provider_queue_seconds == 0
    assert state.policy.usage.provider_rate_limit_wait_seconds == 0
    assert state.policy.usage.provider_backoff_seconds == 0
    assert backend._client is None
    assert runtime._governors == {}
    assert service.take_trace("token", "search-preflight")[0].provider_attempts == []


async def test_broker_runs_bundled_content_preflight_before_transport() -> None:
    class RejectingLocalDocumentBackend(LocalDocumentBackend):
        @staticmethod
        def preflight_fetch(hit: SearchHit) -> None:
            del hit
            raise ProviderRequestError(
                "invalid_request",
                "Local search result cannot be fetched.",
                retryable=False,
            )

    search_backend = LocalSearchBackend("http://provider.invalid")
    document_backend = RejectingLocalDocumentBackend("http://provider.invalid")
    service = _broker_service(
        {"local": search_backend},
        document_backends={"local": document_backend},
    )
    state = service.register_session(make_session())
    handle = DocumentHandle(source="1", docid="1")
    state.remember(
        "local",
        handle,
        identity=document_identity("local", handle),
        rank=1,
    )

    with pytest.raises(CapabilityProviderError) as failed:
        await service.call(
            "token",
            "content.fetch",
            {"source": "1"},
            execution_id="content-preflight",
        )

    assert failed.value.code == "invalid_request"
    assert failed.value.attempts == 0
    assert state.policy.usage.content_fetches == 1
    assert state.policy.usage.content_backend_fetches == 0
    assert state.policy.usage.provider_attempts_by_capability.get("content", 0) == 0
    assert document_backend._client is None
    assert service.take_trace("token", "content-preflight")[0].provider_attempts == []


async def test_fake_http_provider_retries_one_real_local_search() -> None:
    calls: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        calls.append(payload)
        if len(calls) == 1:
            return httpx.Response(
                503,
                headers={"Retry-After": "0"},
                text="secret provider response body",
                request=request,
            )
        return httpx.Response(
            200,
            json={
                "backend": "dense",
                "result_mode": "query_aware",
                "results": [
                    {
                        "query": payload["query"],
                        "hits": [
                            {
                                "docid": "1",
                                "snippet": f"result for {payload['query']}",
                                "rank": 1,
                            }
                        ],
                    }
                ],
            },
            request=request,
        )

    backend = LocalSearchBackend("http://fake-provider.invalid")
    backend._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    runtime = ProviderRuntime(
        ProviderPolicy(
            retry_profile="safe",
            max_attempts=2,
            base_backoff_seconds=0,
        )
    )
    service = _broker_service(
        {"local": backend},
        document_backends={"local": LocalBackend()},
        search_runtime=runtime,
    )
    state = service.register_session(make_session())
    try:
        hits = await service.call(
            "token",
            "search.query",
            {"query": "alpha"},
            execution_id="fake-http-provider",
        )
    finally:
        await service.aclose()

    assert calls == [
        {"query": "alpha", "top_k": 10},
        {"query": "alpha", "top_k": 10},
    ]
    assert hits[0]["source"] == "1"
    assert "docid" not in hits[0]
    assert state.policy.usage.search_calls == 1
    assert state.policy.usage.provider_attempts_by_capability["search"] == 2
    assert state.policy.usage.provider_retries == 1
    trace = service.take_trace("token", "fake-http-provider")[0]
    assert [attempt.status for attempt in trace.provider_attempts] == [
        "error",
        "success",
    ]
    assert "secret" not in trace.model_dump_json()


def test_provider_fingerprint_normalizes_model_mapping_order() -> None:
    first = SearchHit(
        source="doc_1",
        backend="local",
        docid="1",
        rank=1,
        metadata={"alpha": 1, "beta": 2},
    )
    second = SearchHit(
        source="doc_1",
        backend="local",
        docid="1",
        rank=1,
        metadata={"beta": 2, "alpha": 1},
    )

    assert ProviderExecutor.fingerprint(first) == ProviderExecutor.fingerprint(second)


def test_provider_canonical_serialization_preserves_normalization_rules() -> None:
    class ModelLike:
        def model_dump(self, *, mode: str) -> dict[str, int]:
            assert mode == "json"
            return {"beta": 2, "alpha": 1}

    class Fallback:
        def __str__(self) -> str:
            return "fallback"

    cases = [
        ({"beta": 2, "alpha": 1}, b'{"alpha":1,"beta":2}'),
        ({"tags": {"beta", "alpha"}}, b'{"tags":["alpha","beta"]}'),
        ({"model": ModelLike()}, b'{"model":{"alpha":1,"beta":2}}'),
        ({"value": Fallback()}, b'{"value":"fallback"}'),
    ]

    for value, expected in cases:
        encoded = canonical_json_bytes(value)
        digest = hashlib.sha256(encoded).hexdigest()

        assert encoded == expected
        assert ProviderExecutor.fingerprint(value) == f"sha256:v1:{digest}"
        assert ProviderResultCache._encoded_size(value) == len(encoded)


async def test_safe_retry_changes_attempt_usage_not_logical_search_usage() -> None:
    class FlakyWeb(_WebBackendTraits):
        provider_identity = "web:test"

        def __init__(self) -> None:
            self.calls = 0

        async def search(self, query, *, limit, offset=0, domains=None):
            del query, limit, offset, domains
            self.calls += 1
            if self.calls == 1:
                raise httpx.ConnectError("secret endpoint detail")
            return [
                SearchHit(
                    source="",
                    backend="web",
                    url="https://example.com/result",
                    snippet="secret response body must not enter trace",
                    rank=1,
                )
            ]

        async def fetch(self, hit, *, query=None):
            raise AssertionError((hit, query))

    backend = FlakyWeb()
    runtime = ProviderRuntime(
        ProviderPolicy(
            retry_profile="safe",
            max_attempts=2,
            base_backoff_seconds=0,
        )
    )
    service = _broker_service({"web": backend}, search_runtime=runtime)
    state = service.register_session(make_session(backend="web"))

    hits = await service.call(
        "token",
        "search.query",
        {"query": "retry"},
        execution_id="retry-search",
    )

    assert len(hits) == 1
    assert backend.calls == 2
    assert state.policy.usage.search_calls == 1
    assert state.policy.usage.provider_attempts_by_capability["search"] == 2
    assert state.policy.usage.provider_retries == 1
    attempts = service.take_trace("token", "retry-search")[0].provider_attempts
    assert [attempt.status for attempt in attempts] == ["error", "success"]
    assert len({attempt.request_fingerprint for attempt in attempts}) == 1
    assert "secret" not in "".join(attempt.model_dump_json() for attempt in attempts)


async def test_unary_search_preserves_failure_code_and_attempt_count() -> None:
    class MixedWeb(_WebBackendTraits):
        provider_identity = "web:mixed-failures"

        async def search(self, query, *, limit, offset=0, domains=None):
            del limit, offset, domains
            if query == "temporary":
                raise ProviderRequestError(
                    "provider_timeout",
                    "Provider request timed out.",
                    retryable=True,
                )
            raise ProviderRequestError(
                "provider_auth_failed",
                "Provider rejected its configured credentials or permissions.",
                retryable=False,
            )

        async def fetch(self, hit, *, query=None):
            raise AssertionError((hit, query))

    service = _broker_service({"web": MixedWeb()})
    service.register_session(make_session(backend="web"))

    expected = [
        ("temporary", "provider_timeout", True),
        ("permanent", "provider_auth_failed", False),
    ]
    for query, code, retryable in expected:
        with pytest.raises(ProviderRequestError) as failed:
            await service.call("token", "search.query", {"query": query})
        assert failed.value.code == code
        assert failed.value.retryable is retryable
        assert failed.value.attempts == 1


async def test_content_provider_timeout_retries_real_fetch_attempts() -> None:
    class TimeoutContentWeb(WebCacheBackend):
        async def fetch(self, hit, *, query=None):
            del hit, query
            self.fetch_calls += 1
            raise httpx.ReadTimeout("secret provider endpoint")

    backend = TimeoutContentWeb()
    runtime = ProviderRuntime(
        ProviderPolicy(
            retry_profile="safe",
            max_attempts=3,
            base_backoff_seconds=0,
        )
    )
    service = _broker_service({"web": backend}, document_runtime=runtime)
    state = service.register_session(make_session(backend="web"))
    source = (await service.call("token", "search.query", {"query": "timeout"}))[0]["source"]

    with pytest.raises(CapabilityProviderError) as failed:
        await service.call(
            "token",
            "content.fetch",
            {"source": source},
            execution_id="content-timeout-retry",
        )

    assert failed.value.code == "provider_timeout"
    assert failed.value.attempts == 3
    assert backend.fetch_calls == 3
    assert state.policy.usage.provider_attempts_by_capability["content"] == 3
    assert state.policy.usage.provider_retries == 2
    attempts = service.take_trace("token", "content-timeout-retry")[0].provider_attempts
    assert [attempt.attempt for attempt in attempts] == [1, 2, 3]
    assert [attempt.error_code for attempt in attempts] == ["provider_timeout"] * 3


async def test_cancelled_provider_backoff_is_counted_without_a_retry_attempt() -> None:
    waiting = asyncio.Event()

    async def blocking_sleep(_delay: float) -> None:
        waiting.set()
        await asyncio.Event().wait()

    class UnavailableWeb(_WebBackendTraits):
        provider_identity = "web:cancelled-backoff"

        async def search(self, query, *, limit, offset=0, domains=None):
            del query, limit, offset, domains
            raise httpx.ConnectError("secret endpoint")

        async def fetch(self, hit, *, query=None):
            raise AssertionError((hit, query))

    runtime = ProviderRuntime(
        ProviderPolicy(
            retry_profile="safe",
            max_attempts=2,
            base_backoff_seconds=1,
        ),
        sleep=blocking_sleep,
        rng=lambda: 1.0,
    )
    service = _broker_service({"web": UnavailableWeb()}, search_runtime=runtime)
    state = service.register_session(make_session(backend="web"))
    task = asyncio.create_task(
        service.call(
            "token",
            "search.query",
            {"query": "q"},
            execution_id="cancelled-backoff",
        )
    )
    await waiting.wait()
    await asyncio.sleep(0.01)

    await service.cancel_execution("token", "cancelled-backoff")
    with pytest.raises(asyncio.CancelledError):
        await task

    assert state.policy.usage.provider_attempts_by_capability["search"] == 1
    assert state.policy.usage.provider_retries == 0
    assert state.policy.usage.provider_backoff_seconds > 0
    trace = service.take_trace("token", "cancelled-backoff")[0]
    assert trace.status == "cancelled"
    assert len(trace.provider_attempts) == 1


async def test_content_dedupes_sources_and_grep_keeps_failure_indexes() -> None:
    backend = LocalBackend(documents={"1": "target line", "2": "unreadable"})
    backend.failures["2"] = ProviderRequestError(
        "provider_not_found",
        "Provider resource was not found.",
        retryable=False,
    )
    service = _broker_service({"local": backend})
    state = service.register_session(make_session())
    hits = await service.call("token", "search.query", {"query": "q", "limit": 2})
    sources = [hit["source"] for hit in hits]

    report = await service.call(
        "token",
        "content.grep",
        {"sources": [sources[0], sources[1], sources[1]], "pattern": "target"},
        execution_id="grep-report",
    )

    assert report["input_count"] == 3
    assert report["matches"][0]["input_index"] == 0
    assert [row["input_index"] for row in report["source_results"]] == [0]
    assert [row["input_index"] for row in report["failures"]] == [1, 2]
    assert all(row["code"] == "provider_not_found" for row in report["failures"])
    assert backend.fetches == ["1", "2"]
    assert state.policy.usage.content_fetches == 3
    assert state.policy.usage.content_backend_fetches == 2
    trace = service.take_trace("token", "grep-report")[0]
    assert len(trace.provider_attempts) == 2
    assert len(trace.deduplicated_requests) == 1

    only_failure = await service.call(
        "token",
        "content.grep",
        {"sources": [sources[1]], "pattern": "target"},
    )
    assert only_failure["matches"] == []
    assert only_failure["source_results"] == []
    assert only_failure["failures"][0]["code"] == "provider_not_found"


async def test_systemic_content_failure_is_typed_and_never_cached() -> None:
    backend = LocalBackend(documents={"1": "body"})
    backend.failures["1"] = ProviderRequestError(
        "provider_unavailable",
        "Provider is temporarily unavailable.",
        retryable=True,
    )
    service = _broker_service({"local": backend})
    state = service.register_session(make_session())
    source = (await service.call("token", "search.query", {"query": "q"}))[0]["source"]

    with pytest.raises(CapabilityProviderError) as failed:
        await service.call("token", "content.fetch", {"source": source})
    assert failed.value.code == "provider_unavailable"
    assert failed.value.attempts == 1
    assert state.content_cache == {}

    with pytest.raises(CapabilityProviderError) as repeated:
        await service.call("token", "content.fetch", {"source": source})
    assert repeated.value.code == "provider_unavailable"
    assert backend.fetches == ["1", "1"]


async def test_single_content_read_promotes_failure_to_rpc_error() -> None:
    backend = LocalBackend(documents={"1": "body"})
    backend.failures["1"] = ProviderRequestError(
        "provider_unavailable",
        "Provider is temporarily unavailable.",
        retryable=True,
    )
    service = _broker_service({"local": backend})
    service.register_session(make_session())
    source = (await service.call("token", "search.query", {"query": "q"}))[0]["source"]

    with pytest.raises(CapabilityProviderError) as raised:
        await service.call("token", "content.read", {"source": source})

    assert raised.value.code == "provider_unavailable"
    assert raised.value.retryable is True
    assert raised.value.component == "document"


async def test_ambiguous_reader_403_stays_typed() -> None:
    class ForbiddenReaderBackend(LocalBackend):
        name = "web"
        source_kind = "public_url"
        provider_name = "jina_reader"

        async def search(self, query, *, limit, offset=0, domains=None):
            return [
                SearchHit(
                    backend="web",
                    url="https://blocked.example/document",
                    rank=1,
                )
            ]

        async def fetch(self, hit, *, query=None):
            raise ProviderRequestError(
                "provider_auth_failed",
                "Provider rejected its configured credentials or permissions.",
                retryable=False,
                provider_status=403,
            )

    backend = ForbiddenReaderBackend()
    service = _broker_service({"web": backend})
    service.register_session(make_session(backend="web"))
    source = (await service.call("token", "search.query", {"query": "q"}))[0]["source"]

    with pytest.raises(CapabilityProviderError) as failed:
        await service.call("token", "content.fetch", {"source": source})

    assert failed.value.code == "provider_auth_failed"
    assert str(failed.value) == "Provider rejected its configured credentials or permissions."
    assert failed.value.retryable is False
    assert failed.value.attempts == 1
    assert failed.value.provider_status == 403
    assert failed.value.provider == "jina_reader"
    assert failed.value.component == "document"
    assert failed.value.scope == "unknown"


@pytest.mark.parametrize(
    ("code", "attempts"),
    [
        ("provider_http_error", 1),
        ("provider_not_configured", 0),
        ("provider_timeout", 1),
        ("provider_invalid_response", 1),
    ],
)
async def test_permanent_content_failures_remain_aligned_rows(
    code: str,
    attempts: int,
) -> None:
    backend = LocalBackend(documents={"1": "one", "2": "two"})
    backend.failures = {
        docid: ProviderRequestError(
            code,
            "Provider document request could not be completed.",
            retryable=False,
        )
        for docid in backend.documents
    }
    service = _broker_service({"local": backend})
    service.register_session(make_session())
    hits = await service.call("token", "search.query", {"query": "q", "limit": 2})
    sources = [hit["source"] for hit in hits]

    failed = await service.call("token", "content.grep", {"sources": sources, "pattern": "."})

    assert failed["source_results"] == []
    assert [row["code"] for row in failed["failures"]] == [code, code]
    assert [row["attempts"] for row in failed["failures"]] == [attempts, attempts]

    backend.failures.pop("1")
    partial = await service.call("token", "content.grep", {"sources": sources, "pattern": "."})
    assert partial["source_results"][0]["input_index"] == 0
    assert partial["matches"][0]["text"] == "one"
    assert partial["failures"][0]["input_index"] == 1
    assert partial["failures"][0]["code"] == code


async def test_content_deadline_returns_a_typed_timeout() -> None:
    class HangingBackend(LocalBackend):
        async def fetch(self, hit, *, query=None):
            self.fetches.append(str(hit.docid))
            await asyncio.Event().wait()

    backend = HangingBackend(documents={"1": "one"})
    service = _broker_service(
        {"local": backend},
        config=BrokerConfig(content=ContentLimits(batch_deadline_seconds=0.01)),
    )
    service.register_session(make_session())
    source = (await service.call("token", "search.query", {"query": "q"}))[0]["source"]

    with pytest.raises(CapabilityProviderError) as failed:
        await service.call("token", "content.fetch", {"source": source})

    assert failed.value.code == "content_deadline_exceeded"
    assert str(failed.value) == "The content batch deadline was exceeded."
    assert failed.value.retryable is True
    assert failed.value.attempts == 1
    assert failed.value.provider == "local"
    assert failed.value.component == "document"
    assert backend.fetches == ["1"]


async def test_internet_archive_text_fallback_is_separately_accounted() -> None:
    class ArchiveBackend(_WebBackendTraits):
        provider_identity = "archive-test"
        max_depth = 10

        def __init__(self) -> None:
            self.fetches: list[str] = []

        async def search(self, query, *, limit, offset=0, domains=None):
            del query, limit, offset, domains
            return [
                SearchHit(
                    backend="web",
                    url="https://archive.org/details/example_book",
                    rank=1,
                )
            ]

        async def fetch(self, hit, *, query=None):
            del query
            url = str(hit.url)
            self.fetches.append(url)
            if url.endswith("/details/example_book"):
                raise ProviderRequestError(
                    "provider_rejected",
                    "Provider rejected the request.",
                    retryable=False,
                    provider_status=422,
                )
            return DocumentContent(source=hit.source, text="archive text", url=hit.url)

        @staticmethod
        def fetch_candidates(hit: DocumentHandle) -> list[DocumentHandle]:
            return document_fetch_candidates(hit)

    backend = ArchiveBackend()
    service = _broker_service({"web": backend})
    state = service.register_session(make_session(backend="web"))
    source = (await service.call("token", "search.query", {"query": "book"}))[0]["source"]

    document = await service.call(
        "token",
        "content.fetch",
        {"source": source},
        execution_id="archive-fallback",
    )

    assert document["text"] == "archive text"
    assert document["source"] == source
    assert document["metadata"]["representation"] == "internet_archive_djvu_text"
    assert backend.fetches == [
        "https://archive.org/details/example_book",
        "https://archive.org/download/example_book/example_book_djvu.txt",
    ]
    assert state.policy.usage.content_backend_fetches == 2
    trace = service.take_trace("token", "archive-fallback")[0]
    assert len(trace.provider_attempts) == 2


async def test_content_source_limit_rejects_before_usage_or_provider_side_effect() -> None:
    backend = LocalBackend()
    service = _broker_service(
        {"local": backend},
        config=BrokerConfig(content=ContentLimits(max_sources_per_request=2)),
    )
    state = service.register_session(make_session())
    source = (await service.call("token", "search.query", {"query": "q"}))[0]["source"]

    with pytest.raises(ValueError, match="3 sources"):
        await service.call(
            "token",
            "content.grep",
            {"sources": [source, source, source], "pattern": "body"},
        )

    assert state.policy.usage.content_fetches == 0
    assert state.policy.usage.content_backend_fetches == 0
    assert backend.fetches == []


async def test_content_cache_budget_counts_utf8_bytes() -> None:
    backend = LocalBackend(documents={"1": "é"})
    service = _broker_service(
        {"local": backend},
        config=BrokerConfig(content=ContentLimits(session_cache_bytes=1)),
    )
    service.register_session(make_session())
    source = (await service.call("token", "search.query", {"query": "q"}))[0]["source"]

    await service.call("token", "content.fetch", {"source": source})
    await service.call("token", "content.fetch", {"source": source})

    # The passage is one Unicode code point but two UTF-8 bytes, so it cannot
    # enter a one-byte cache and must be fetched again.
    assert backend.fetches == ["1", "1"]


async def test_provider_result_cache_is_bounded_lru_ttl_and_returns_copies() -> None:
    now = [0.0]
    cache = ProviderResultCache(
        ttl_seconds=1.0,
        max_bytes=8,
        clock=lambda: now[0],
    )

    assert await cache.put("first", {"v": 1}) is True
    hit, first = await cache.get("first")
    assert hit is True
    first["v"] = 2
    assert (await cache.get("first"))[1] == {"v": 1}

    # Each compact object is seven bytes; inserting the second evicts the LRU.
    assert await cache.put("second", {"v": 2}) is True
    assert (await cache.get("first"))[0] is False
    assert cache.snapshot()["evictions"] == 1

    now[0] = 2.0
    assert (await cache.get("second"))[0] is False
    assert cache.snapshot()["evictions"] == 2
    assert cache.key("provider-a", "search", "request") != cache.key(
        "provider-b", "search", "request"
    )


async def test_web_provider_cache_reuses_search_and_scrape_across_sessions() -> None:
    backend = WebCacheBackend()
    service = _broker_service(
        {"web": backend},
        provider_execution_config=ProviderExecutionConfig(
            result_cache_ttl_seconds=300,
            result_cache_max_bytes=1_000_000,
        ),
    )
    first = service.register_session(
        make_session(backend="web", session_id="first", token="first-token")
    )
    second = service.register_session(
        make_session(backend="web", session_id="second", token="second-token")
    )
    try:
        first_source = (
            await service.call(
                "first-token",
                "search.query",
                {"query": "cached"},
                execution_id="first-search",
            )
        )[0]["source"]
        second_source = (
            await service.call(
                "second-token",
                "search.query",
                {"query": "cached"},
                execution_id="second-search",
            )
        )[0]["source"]
        await service.call(
            "first-token",
            "content.fetch",
            {"source": first_source},
            execution_id="first-content",
        )
        document = await service.call(
            "second-token",
            "content.fetch",
            {"source": second_source},
            execution_id="second-content",
        )
    finally:
        await service.aclose()

    assert document["text"] == f"body for {first_source}"
    assert backend.search_calls == 1
    assert backend.fetch_calls == 1
    assert first.policy.usage.provider_attempts_by_capability["search"] == 1
    assert first.policy.usage.content_backend_fetches == 1
    assert second.policy.usage.provider_attempts_by_capability.get("search", 0) == 0
    assert second.policy.usage.content_backend_fetches == 0
    assert second.policy.usage.provider_cache_hits == 2
    search_trace = service.take_trace("second-token", "second-search")[0]
    content_trace = service.take_trace("second-token", "second-content")[0]
    assert search_trace.provider_attempts == []
    assert content_trace.provider_attempts == []
    assert search_trace.provider_cache_hits == 1
    assert content_trace.provider_cache_hits == 1


async def test_provider_cache_key_includes_backend_revision() -> None:
    backend = WebCacheBackend()
    service = _broker_service(
        {"web": backend},
        backend_revision="revision-a",
        provider_execution_config=ProviderExecutionConfig(result_cache_ttl_seconds=300),
    )
    service.register_session(make_session(backend="web", session_id="a", token="a"))
    service.register_session(make_session(backend="web", session_id="b", token="b"))
    try:
        await service.call("a", "search.query", {"query": "revision"})
        service.search_bindings["web"] = replace(
            service.search_bindings["web"],
            revision="revision-b",
        )
        await service.call("b", "search.query", {"query": "revision"})
    finally:
        await service.aclose()

    assert backend.search_calls == 2


async def test_provider_cache_coalesces_concurrent_cross_session_misses() -> None:
    backend = WebCacheBackend()
    backend.block_first = True
    service = _broker_service(
        {"web": backend},
        provider_execution_config=ProviderExecutionConfig(result_cache_ttl_seconds=300),
    )
    first = service.register_session(make_session(backend="web", session_id="a", token="a"))
    second = service.register_session(make_session(backend="web", session_id="b", token="b"))
    first_call = asyncio.create_task(
        service.call("a", "search.query", {"query": "shared"}, execution_id="a-search")
    )
    await backend.started.wait()
    second_call = asyncio.create_task(
        service.call("b", "search.query", {"query": "shared"}, execution_id="b-search")
    )
    for _ in range(100):
        if service.providers.result_cache.snapshot()["waiting"] == 1:
            break
        await asyncio.sleep(0)
    backend.release.set()
    try:
        first_rows, second_rows = await asyncio.gather(first_call, second_call)
    finally:
        await service.aclose()

    assert first_rows == second_rows
    assert backend.search_calls == 1
    assert first.policy.usage.provider_cache_misses == 1
    assert second.policy.usage.provider_cache_misses == 1
    attempts = sum(
        state.policy.usage.provider_attempts_by_capability.get("search", 0)
        for state in (first, second)
    )
    coalesced = (
        first.policy.usage.provider_coalesced_requests
        + second.policy.usage.provider_coalesced_requests
    )
    assert attempts == 1
    assert coalesced == 1
    snapshot = service.providers.result_cache.snapshot()
    assert snapshot["coalesced_waiters"] == 1
    traces = [
        service.take_trace("a", "a-search")[0],
        service.take_trace("b", "b-search")[0],
    ]
    assert sorted(len(trace.provider_attempts) for trace in traces) == [0, 1]
    assert sum(len(trace.coalesced_requests) for trace in traces) == 1


async def test_provider_cache_does_not_store_failures_and_recovers_from_cancellation() -> None:
    backend = WebCacheBackend()
    backend.failures_remaining = 1
    service = _broker_service(
        {"web": backend},
        provider_execution_config=ProviderExecutionConfig(result_cache_ttl_seconds=300),
    )
    service.register_session(make_session(backend="web", session_id="a", token="a"))
    service.register_session(make_session(backend="web", session_id="b", token="b"))
    try:
        with pytest.raises(ProviderRequestError):
            await service.call("a", "search.query", {"query": "retry"})
        rows = await service.call("b", "search.query", {"query": "retry"})
    finally:
        await service.aclose()

    assert rows[0]["source"] == "https://example.com/retry"
    assert backend.search_calls == 2

    cancelling_backend = WebCacheBackend()
    cancelling_backend.block_first = True
    cancelling_service = _broker_service(
        {"web": cancelling_backend},
        provider_execution_config=ProviderExecutionConfig(result_cache_ttl_seconds=300),
    )
    cancelling_service.register_session(
        make_session(backend="web", session_id="cancel-a", token="cancel-a")
    )
    cancelling_service.register_session(
        make_session(backend="web", session_id="cancel-b", token="cancel-b")
    )
    cancelled = asyncio.create_task(
        cancelling_service.call("cancel-a", "search.query", {"query": "cancel"})
    )
    await cancelling_backend.started.wait()
    follower = asyncio.create_task(
        cancelling_service.call("cancel-b", "search.query", {"query": "cancel"})
    )
    for _ in range(100):
        if cancelling_service.providers.result_cache.snapshot()["waiting"] == 1:
            break
        await asyncio.sleep(0)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    cancelling_backend.release.set()
    try:
        recovered = await follower
    finally:
        await cancelling_service.aclose()

    assert recovered[0]["source"] == "https://example.com/cancel"
    assert cancelling_backend.search_calls == 2
