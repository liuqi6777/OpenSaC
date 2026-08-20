from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from opensac._contracts import ContentSnippet, SearchBatch, SearchHit
from opensac.backends.search.local_http import LocalSearchBackend
from opensac.backends.search.serper import SerperBackend
from opensac.broker.documents import document_identity
from opensac.broker.provider_execution import CapabilityProviderError, ProviderExecutor
from opensac.broker.service import BrokerService
from opensac.models import Mechanisms, ResourceBudget, Session
from opensac.provider import ProviderPolicy, ProviderRequestError, ProviderRuntime


def make_session(*, backend: str = "local") -> Session:
    return Session(
        id="sess_provider",
        token="token",
        backends=[backend],
        workspace="/tmp/provider-session",
        mechanisms=Mechanisms(),
        budget=ResourceBudget(),
    )


class LocalBackend:
    name = "local"
    supports_domains = False
    max_depth = None
    provider_identity = "local:test"

    def __init__(self, *, documents: dict[str, str] | None = None) -> None:
        self.search_many_calls: list[list[str]] = []
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

    async def search_many(self, queries, *, limit, offset=0, domains=None):
        del domains
        self.search_many_calls.append(list(queries))
        return [
            SearchBatch(
                query=query,
                hits=await self.search(query, limit=limit, offset=offset),
            )
            for query in queries
        ]

    async def fetch(self, hit, *, query=None):
        del query
        docid = str(hit.docid)
        self.fetches.append(docid)
        if failure := self.failures.get(docid):
            raise failure
        return ContentSnippet(
            source=hit.source,
            text=self.documents[docid],
            title=hit.title,
            metadata={"backend": "local", "docid": docid},
        )


async def test_broker_runs_bundled_search_preflight_before_provider_usage() -> None:
    backend = SerperBackend("")
    runtime = ProviderRuntime(
        {
            "web.search": ProviderPolicy(
                requests_per_second=1.0,
                burst=1,
            )
        }
    )
    service = BrokerService({"web": backend}, provider_runtime=runtime)
    state = service.register_session(make_session(backend="web"))

    with pytest.raises(CapabilityProviderError) as caught:
        await service.call(
            "token",
            "search.query_many",
            {"queries": ["alpha", "beta"]},
            execution_id="search-preflight",
        )

    assert caught.value.code == "provider_not_configured"
    assert caught.value.attempts == 0
    assert state.policy.usage.search_calls == 2
    assert state.policy.usage.search_provider_attempts == 0
    assert state.policy.usage.provider_retries == 0
    assert state.policy.usage.provider_queue_seconds == 0
    assert state.policy.usage.provider_rate_limit_wait_seconds == 0
    assert state.policy.usage.provider_backoff_seconds == 0
    assert backend._client is None
    assert runtime._governors == {}
    assert service.take_trace("token", "search-preflight")[0].provider_attempts == []


async def test_broker_runs_bundled_content_preflight_before_transport() -> None:
    class RejectingLocalBackend(LocalSearchBackend):
        @staticmethod
        def preflight_fetch(hit: SearchHit) -> None:
            del hit
            raise ProviderRequestError(
                "invalid_request",
                "Local search result cannot be fetched.",
                retryable=False,
            )

    backend = RejectingLocalBackend("http://provider.invalid")
    service = BrokerService({"local": backend})
    state = service.register_session(make_session())
    hit = SearchHit(
        source="1",
        backend="local",
        docid="1",
        rank=1,
    )
    state.remember(
        hit,
        identity=document_identity(hit),
        candidate_source="1",
    )

    rows = await service.call(
        "token",
        "content.get_many",
        {"sources": ["1"]},
        execution_id="content-preflight",
    )

    assert rows[0]["failure"]["code"] == "invalid_request"
    assert rows[0]["failure"]["attempts"] == 0
    assert rows[0]["text"] == ""
    assert state.policy.usage.content_fetches == 1
    assert state.policy.usage.content_backend_fetches == 0
    assert state.policy.usage.content_provider_attempts == 0
    assert backend._client is None
    assert service.take_trace("token", "content-preflight")[0].provider_attempts == []


async def test_fake_http_provider_retries_one_real_local_microbatch() -> None:
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
                        "query": query,
                        "hits": [
                            {
                                "docid": str(index),
                                "snippet": f"result for {query}",
                                "rank": 1,
                            }
                        ],
                    }
                    for index, query in enumerate(payload["queries"], start=1)
                ],
            },
            request=request,
        )

    backend = LocalSearchBackend("http://fake-provider.invalid")
    backend._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    runtime = ProviderRuntime(
        {
            "local.search": ProviderPolicy(
                retry_profile="safe",
                max_attempts=2,
                base_backoff_seconds=0,
            )
        }
    )
    service = BrokerService({"local": backend}, provider_runtime=runtime)
    state = service.register_session(make_session())
    try:
        rows = await service.call(
            "token",
            "search.query_many",
            {"queries": ["alpha", "alpha", "beta"]},
            execution_id="fake-http-provider",
        )
    finally:
        await service.aclose()

    assert calls == [
        {"queries": ["alpha", "beta"], "top_k": 10},
        {"queries": ["alpha", "beta"], "top_k": 10},
    ]
    assert [row["query"] for row in rows] == ["alpha", "alpha", "beta"]
    assert [row["hits"][0]["source"] for row in rows] == ["1", "1", "2"]
    assert all("docid" not in row["hits"][0] for row in rows)
    assert state.policy.usage.search_calls == 3
    assert state.policy.usage.search_provider_attempts == 2
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


async def test_local_query_many_deduplicates_before_one_transport_microbatch() -> None:
    backend = LocalBackend()
    service = BrokerService(
        {"local": backend},
        max_search_queries_per_request=128,
    )
    state = service.register_session(make_session())

    rows = await service.call(
        "token",
        "search.query_many",
        {"queries": ["same"] * 100},
        execution_id="dedupe-search",
    )

    assert len(rows) == 100
    assert all(row["query"] == "same" and len(row["hits"]) == 1 for row in rows)
    assert backend.search_many_calls == [["same"]]
    assert state.policy.usage.search_calls == 100
    assert state.policy.usage.search_provider_attempts == 1
    assert state.policy.usage.intra_call_deduplicated_items == 99
    trace = service.take_trace("token", "dedupe-search")[0]
    assert trace.provider_attempts[0].request_indexes == [0]
    assert trace.provider_attempts[0].response_fingerprint is not None
    assert len(trace.deduplicated_requests) == 99


async def test_safe_retry_changes_attempt_usage_not_logical_search_usage() -> None:
    class FlakyWeb:
        name = "web"
        supports_domains = True
        max_depth = 100
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
        {
            "web.search": ProviderPolicy(
                retry_profile="safe",
                max_attempts=2,
                base_backoff_seconds=0,
            )
        }
    )
    service = BrokerService({"web": backend}, provider_runtime=runtime)
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
    assert state.policy.usage.search_provider_attempts == 2
    assert state.policy.usage.provider_retries == 1
    attempts = service.take_trace("token", "retry-search")[0].provider_attempts
    assert [attempt.status for attempt in attempts] == ["error", "success"]
    assert len({attempt.request_fingerprint for attempt in attempts}) == 1
    assert "secret" not in "".join(attempt.model_dump_json() for attempt in attempts)


async def test_promoted_batch_uses_nonretryable_failure_and_total_attempts() -> None:
    class MixedWeb:
        name = "web"
        supports_domains = True
        max_depth = 100
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

    service = BrokerService({"web": MixedWeb()})
    service.register_session(make_session(backend="web"))

    with pytest.raises(CapabilityProviderError) as caught:
        await service.call(
            "token",
            "search.query_many",
            {"queries": ["temporary", "permanent"]},
        )

    assert caught.value.code == "provider_auth_failed"
    assert caught.value.retryable is False
    assert caught.value.attempts == 2


async def test_cancelled_provider_backoff_is_counted_without_a_retry_attempt() -> None:
    waiting = asyncio.Event()

    async def blocking_sleep(_delay: float) -> None:
        waiting.set()
        await asyncio.Event().wait()

    class UnavailableWeb:
        name = "web"
        supports_domains = True
        max_depth = 100
        provider_identity = "web:cancelled-backoff"

        async def search(self, query, *, limit, offset=0, domains=None):
            del query, limit, offset, domains
            raise httpx.ConnectError("secret endpoint")

        async def fetch(self, hit, *, query=None):
            raise AssertionError((hit, query))

    runtime = ProviderRuntime(
        {
            "web.search": ProviderPolicy(
                retry_profile="safe",
                max_attempts=2,
                base_backoff_seconds=1,
            )
        },
        sleep=blocking_sleep,
        rng=lambda: 1.0,
    )
    service = BrokerService({"web": UnavailableWeb()}, provider_runtime=runtime)
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

    assert state.policy.usage.search_provider_attempts == 1
    assert state.policy.usage.provider_retries == 0
    assert state.policy.usage.provider_backoff_seconds > 0
    trace = service.take_trace("token", "cancelled-backoff")[0]
    assert trace.status == "cancelled"
    assert len(trace.provider_attempts) == 1


async def test_content_dedupes_sources_and_grep_report_keeps_failure_indexes() -> None:
    backend = LocalBackend(documents={"1": "target line", "2": "unreadable"})
    backend.failures["2"] = ProviderRequestError(
        "provider_not_found",
        "Provider resource was not found.",
        retryable=False,
    )
    service = BrokerService({"local": backend})
    state = service.register_session(make_session())
    hits = await service.call("token", "search.query", {"query": "q", "limit": 2})
    sources = [hit["source"] for hit in hits]

    report = await service.call(
        "token",
        "content.grep_report",
        {"sources": [sources[0], sources[1], sources[1]], "pattern": "target"},
        execution_id="grep-report",
    )

    assert report["input_count"] == 3
    assert report["matches"][0]["input_index"] == 0
    assert [failure["input_index"] for failure in report["failures"]] == [1, 2]
    assert all(failure["failure"]["code"] == "provider_not_found" for failure in report["failures"])
    assert backend.fetches == ["1", "2"]
    assert state.policy.usage.content_fetches == 3
    assert state.policy.usage.content_backend_fetches == 2
    trace = service.take_trace("token", "grep-report")[0]
    assert len(trace.provider_attempts) == 2
    assert len(trace.deduplicated_requests) == 1

    only_failure = await service.call(
        "token",
        "content.grep_report",
        {"sources": [sources[1]], "pattern": "target"},
    )
    assert only_failure["matches"] == []
    assert only_failure["failures"][0]["failure"]["code"] == "provider_not_found"


async def test_systemic_content_failure_is_promoted_and_never_cached() -> None:
    backend = LocalBackend(documents={"1": "body"})
    backend.failures["1"] = ProviderRequestError(
        "provider_unavailable",
        "Provider is temporarily unavailable.",
        retryable=True,
    )
    service = BrokerService({"local": backend})
    state = service.register_session(make_session())
    source = (await service.call("token", "search.query", {"query": "q"}))[0]["source"]

    with pytest.raises(CapabilityProviderError) as caught:
        await service.call("token", "content.get_many", {"sources": [source]})
    assert caught.value.code == "provider_unavailable"
    assert caught.value.attempts == 1
    assert state.content_cache == {}

    with pytest.raises(CapabilityProviderError):
        await service.call("token", "content.get_many", {"sources": [source]})
    assert backend.fetches == ["1", "1"]


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
            "Provider document operation could not be completed.",
            retryable=False,
        )
        for docid in backend.documents
    }
    service = BrokerService({"local": backend})
    service.register_session(make_session())
    hits = await service.call("token", "search.query", {"query": "q", "limit": 2})
    sources = [hit["source"] for hit in hits]

    failed = await service.call("token", "content.get_many", {"sources": sources})

    assert [row["failure"]["code"] for row in failed] == [code, code]
    assert [row["failure"]["attempts"] for row in failed] == [attempts, attempts]
    assert all(row["text"] == "" for row in failed)

    backend.failures.pop("1")
    partial = await service.call("token", "content.get_many", {"sources": sources})
    assert partial[0]["text"] == "one"
    assert partial[0].get("failure") is None
    assert partial[1]["failure"]["code"] == code


async def test_content_batch_deadline_returns_an_aligned_timeout_row() -> None:
    class HangingBackend(LocalBackend):
        async def fetch(self, hit, *, query=None):
            self.fetches.append(str(hit.docid))
            await asyncio.Event().wait()

    backend = HangingBackend(documents={"1": "one"})
    service = BrokerService(
        {"local": backend},
        content_batch_deadline_seconds=0.01,
    )
    service.register_session(make_session())
    source = (await service.call("token", "search.query", {"query": "q"}))[0]["source"]

    rows = await service.call("token", "content.get_many", {"sources": [source]})

    assert rows[0]["failure"] == {
        "code": "content_deadline_exceeded",
        "message": "The content batch deadline was exceeded.",
        "retryable": True,
        "attempts": 1,
        "provider_status": None,
        "retry_after_seconds": None,
    }
    assert backend.fetches == ["1"]


async def test_internet_archive_text_fallback_is_separately_accounted() -> None:
    class ArchiveBackend:
        name = "web"
        provider_identity = "archive-test"
        supports_domains = True
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
            return ContentSnippet(source=hit.source, text="archive text", url=hit.url)

    backend = ArchiveBackend()
    service = BrokerService({"web": backend})
    state = service.register_session(make_session(backend="web"))
    source = (await service.call("token", "search.query", {"query": "book"}))[0]["source"]

    rows = await service.call(
        "token",
        "content.get_many",
        {"sources": [source]},
        execution_id="archive-fallback",
    )

    assert rows[0]["text"] == "archive text"
    assert rows[0]["source"] == source
    assert rows[0]["metadata"]["representation"] == "internet_archive_djvu_text"
    assert backend.fetches == [
        "https://archive.org/details/example_book",
        "https://archive.org/download/example_book/example_book_djvu.txt",
    ]
    assert state.policy.usage.content_backend_fetches == 2
    trace = service.take_trace("token", "archive-fallback")[0]
    assert len(trace.provider_attempts) == 2


async def test_content_source_limit_rejects_before_usage_or_provider_side_effect() -> None:
    backend = LocalBackend()
    service = BrokerService({"local": backend}, max_content_sources_per_request=2)
    state = service.register_session(make_session())
    source = (await service.call("token", "search.query", {"query": "q"}))[0]["source"]

    with pytest.raises(ValueError, match="3 sources"):
        await service.call(
            "token",
            "content.get_many",
            {"sources": [source, source, source]},
        )

    assert state.policy.usage.content_fetches == 0
    assert state.policy.usage.content_backend_fetches == 0
    assert backend.fetches == []


async def test_content_cache_budget_counts_utf8_bytes() -> None:
    backend = LocalBackend(documents={"1": "é"})
    service = BrokerService({"local": backend}, session_content_cache_bytes=1)
    service.register_session(make_session())
    source = (await service.call("token", "search.query", {"query": "q"}))[0]["source"]

    await service.call("token", "content.get_many", {"sources": [source]})
    await service.call("token", "content.get_many", {"sources": [source]})

    # The passage is one Unicode code point but two UTF-8 bytes, so it cannot
    # enter a one-byte cache and must be fetched again.
    assert backend.fetches == ["1", "1"]
