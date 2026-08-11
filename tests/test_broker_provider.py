from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from opensac_sdk.models import ContentSnippet, SearchBatch, SearchHit

from opensac.backends.local_http import LocalSearchBackend
from opensac.backends.serper import SerperBackend
from opensac.broker.service import (
    BrokerService,
    CapabilityProviderError,
    EvidenceRecord,
)
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
            ref="",
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
            ref=hit.ref,
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
    backend = LocalSearchBackend("http://provider.invalid")
    service = BrokerService({"local": backend})
    state = service.register_session(make_session())
    state.remember(
        SearchHit(
            ref="ref_without_docid",
            backend="local",
            docid=None,
            rank=1,
        )
    )

    rows = await service.call(
        "token",
        "content.get_many",
        {"refs": ["ref_without_docid"]},
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
    assert [row["hits"][0]["docid"] for row in rows] == ["1", "1", "2"]
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
        ref="ref_x",
        backend="local",
        docid="1",
        rank=1,
        metadata={"alpha": 1, "beta": 2},
    )
    second = SearchHit(
        ref="ref_x",
        backend="local",
        docid="1",
        rank=1,
        metadata={"beta": 2, "alpha": 1},
    )

    assert BrokerService._fingerprint(first) == BrokerService._fingerprint(second)


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
                    ref="",
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


async def test_content_dedupes_refs_and_grep_report_keeps_failure_indexes() -> None:
    backend = LocalBackend(documents={"1": "target line", "2": "unreadable"})
    backend.failures["2"] = ProviderRequestError(
        "provider_not_found",
        "Provider resource was not found.",
        retryable=False,
    )
    service = BrokerService({"local": backend})
    state = service.register_session(make_session())
    hits = await service.call("token", "search.query", {"query": "q", "limit": 2})
    refs = [hit["ref"] for hit in hits]

    report = await service.call(
        "token",
        "content.grep_report",
        {"refs": [refs[0], refs[1], refs[1]], "pattern": "target"},
        execution_id="grep-report",
    )

    assert report["input_count"] == 3
    assert report["matches"][0]["input_index"] == 0
    assert [failure["input_index"] for failure in report["failures"]] == [1, 2]
    assert all(
        failure["failure"]["code"] == "provider_not_found"
        for failure in report["failures"]
    )
    assert backend.fetches == ["1", "2"]
    assert state.policy.usage.content_fetches == 3
    assert state.policy.usage.content_backend_fetches == 2
    trace = service.take_trace("token", "grep-report")[0]
    assert len(trace.provider_attempts) == 2
    assert len(trace.deduplicated_requests) == 1

    only_failure = await service.call(
        "token",
        "content.grep_report",
        {"refs": [refs[1]], "pattern": "target"},
    )
    assert only_failure["matches"] == []
    assert only_failure["failures"][0]["failure"]["code"] == "provider_not_found"
    with pytest.raises(CapabilityProviderError) as legacy:
        await service.call(
            "token",
            "content.grep",
            {"refs": [refs[1]], "pattern": "target"},
        )
    assert legacy.value.code == "provider_not_found"


async def test_systemic_content_failure_is_promoted_and_never_cached() -> None:
    backend = LocalBackend(documents={"1": "body"})
    backend.failures["1"] = ProviderRequestError(
        "provider_unavailable",
        "Provider is temporarily unavailable.",
        retryable=True,
    )
    service = BrokerService({"local": backend})
    state = service.register_session(make_session())
    ref = (await service.call("token", "search.query", {"query": "q"}))[0]["ref"]

    with pytest.raises(CapabilityProviderError) as caught:
        await service.call("token", "content.get_many", {"refs": [ref]})
    assert caught.value.code == "provider_unavailable"
    assert caught.value.attempts == 1
    assert state.content_cache == {}

    with pytest.raises(CapabilityProviderError):
        await service.call("token", "content.get_many", {"refs": [ref]})
    assert backend.fetches == ["1", "1"]


@pytest.mark.parametrize(
    ("code", "attempts"),
    [
        ("provider_http_error", 1),
        ("provider_not_configured", 0),
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
    refs = [hit["ref"] for hit in hits]

    failed = await service.call("token", "content.get_many", {"refs": refs})

    assert [row["failure"]["code"] for row in failed] == [code, code]
    assert [row["failure"]["attempts"] for row in failed] == [attempts, attempts]
    assert all(row["text"] == "" for row in failed)

    backend.failures.pop("1")
    partial = await service.call("token", "content.get_many", {"refs": refs})
    assert partial[0]["text"] == "one"
    assert partial[0].get("failure") is None
    assert partial[1]["failure"]["code"] == code


async def test_evidence_capacity_uses_utf8_bytes_and_keeps_old_locators() -> None:
    backend = LocalBackend(documents={"1": "é", "2": "a"})
    service = BrokerService(
        {"local": backend},
        max_evidence_records=1,
        max_evidence_passage_bytes=2,
    )
    state = service.register_session(make_session())
    hits = await service.call("token", "search.query", {"query": "q", "limit": 2})

    first = await service.call(
        "token",
        "content.read",
        {"refs": [hits[0]["ref"]], "limit": 1},
    )
    duplicate = await service.call(
        "token",
        "content.read",
        {"refs": [hits[0]["ref"]], "limit": 1},
    )
    exhausted = await service.call(
        "token",
        "content.read",
        {"refs": [hits[1]["ref"]], "limit": 1},
    )

    assert first[0]["locator"] == duplicate[0]["locator"]
    assert exhausted[0]["text"] == "a"
    assert exhausted[0].get("locator") is None
    assert exhausted[0]["locator_error"] == {
        "code": "evidence_capacity_exhausted",
        "message": "The session evidence registry is full.",
        "retryable": False,
    }
    assert state.policy.usage.evidence_records == 1
    assert state.policy.usage.evidence_passage_bytes == 2
    selected = await service.call(
        "token",
        "citations.resolve",
        {
            "requests": [
                {"ref": hits[0]["ref"], "locator": first[0]["locator"]}
            ]
        },
    )
    assert selected[0]["evidence"] == "é"
    with pytest.raises(ValueError, match="explicit null"):
        await service.call(
            "token",
            "citations.resolve",
            {"requests": [{"ref": hits[1]["ref"], "locator": None}]},
        )


def test_evidence_locator_collision_never_overwrites_the_existing_binding() -> None:
    service = BrokerService({"local": LocalBackend()})
    state = service.register_session(make_session())
    locator, error = service._register_evidence(
        state,
        ref="ref_x",
        text="original",
        document_text="document",
        coordinates={"type": "lines", "start_line": 1, "end_line": 1},
    )
    assert locator is not None and error is None
    conflicting = EvidenceRecord(
        ref="ref_x",
        kind="selected_passage",
        text="conflicting",
        coordinates={"type": "lines", "start_line": 9, "end_line": 9},
        document_fingerprint="d" * 64,
        passage_fingerprint="p" * 64,
    )
    state.evidence[locator["id"]] = conflicting

    with pytest.raises(RuntimeError, match="collision"):
        service._register_evidence(
            state,
            ref="ref_x",
            text="original",
            document_text="document",
            coordinates={"type": "lines", "start_line": 1, "end_line": 1},
        )

    assert state.evidence[locator["id"]] is conflicting


async def test_evidence_capacity_admission_is_atomic_across_concurrent_reads() -> None:
    backend = LocalBackend(documents={"1": "one", "2": "two"})
    service = BrokerService(
        {"local": backend},
        max_evidence_records=1,
        max_evidence_passage_bytes=100,
    )
    state = service.register_session(make_session())
    hits = await service.call("token", "search.query", {"query": "q", "limit": 2})

    returned = await asyncio.gather(
        *(
            service.call(
                "token",
                "content.read",
                {"refs": [hit["ref"]], "limit": 1},
            )
            for hit in hits
        )
    )
    rows = [batch[0] for batch in returned]

    assert sum(row.get("locator") is not None for row in rows) == 1
    assert sum(row.get("locator_error") is not None for row in rows) == 1
    assert len(state.evidence) == state.policy.usage.evidence_records == 1
    assert state.evidence_passage_bytes <= 100


async def test_content_ref_limit_rejects_before_usage_or_provider_side_effect() -> None:
    backend = LocalBackend()
    service = BrokerService({"local": backend}, max_content_refs_per_request=2)
    state = service.register_session(make_session())
    ref = (await service.call("token", "search.query", {"query": "q"}))[0]["ref"]

    with pytest.raises(ValueError, match="3 refs"):
        await service.call(
            "token",
            "content.get_many",
            {"refs": [ref, ref, ref]},
        )

    assert state.policy.usage.content_fetches == 0
    assert state.policy.usage.content_backend_fetches == 0
    assert backend.fetches == []


async def test_content_cache_budget_counts_utf8_bytes() -> None:
    backend = LocalBackend(documents={"1": "é"})
    service = BrokerService({"local": backend}, session_content_cache_bytes=1)
    service.register_session(make_session())
    ref = (await service.call("token", "search.query", {"query": "q"}))[0]["ref"]

    await service.call("token", "content.get_many", {"refs": [ref]})
    await service.call("token", "content.get_many", {"refs": [ref]})

    # The passage is one Unicode code point but two UTF-8 bytes, so it cannot
    # enter a one-byte cache and must be fetched again.
    assert backend.fetches == ["1", "1"]
