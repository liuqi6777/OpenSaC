from __future__ import annotations

import importlib
import inspect
import json
import threading
import time
from pathlib import Path
from unittest.mock import patch

import httpx
import opensac_sdk
import pytest
from opensac_sdk._diagnostics import error_info, failure_status
from opensac_sdk._many import (
    _SYSTEM_FAILURE_CODES,
    _ManyFailure,
    _ManySuccess,
    _run_many,
)
from opensac_sdk._record import Record, record, wrap
from opensac_sdk._resources import (
    CapabilitiesResource,
    ContentResource,
    LLMResource,
    SearchResource,
    StateResource,
)
from opensac_sdk._surface import (
    SDK_SURFACE,
    SDK_TRANSPORT_METHODS,
    SurfaceTier,
)
from opensac_sdk.transport import BrokerError, UnixSocketTransport

RESOURCE_TYPES = {
    "capabilities": CapabilitiesResource,
    "content": ContentResource,
    "llm": LLMResource,
    "search": SearchResource,
    "state": StateResource,
}


def test_package_root_exposes_only_runtime_entrypoints() -> None:
    assert opensac_sdk.__all__ == ["BrokerError", "sdk", "__version__"]
    assert not hasattr(opensac_sdk, "SearchHit")
    assert not hasattr(opensac_sdk, "OpenSACClient")
    assert not hasattr(opensac_sdk, "LazyOpenSACClient")
    removed_modules = [
        "citations",
        "content",
        "llm",
        "models",
        "output",
        "search",
        "session",
        "state",
        "types",
    ]
    for module in removed_modules:
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(f"opensac_sdk.{module}")


def test_sdk_package_publishes_typing_metadata() -> None:
    package = Path(opensac_sdk.__file__).parent
    assert (package / "py.typed").is_file()
    stub = package / "__init__.pyi"
    assert stub.is_file()
    text = stub.read_text(encoding="utf-8")
    assert "def fetch(" in text
    assert "def fetch_many(" in text
    assert "def extract(" in text
    assert "def extract_many(" in text
    assert "def read_many(" not in text
    assert "def usage(" not in text
    assert "def submit(" not in text
    assert "capabilities: _CapabilitiesResource" in text
    assert "output: _" not in text
    assert "session: _" not in text


def test_surface_manifest_covers_every_sdk_resource_method_once() -> None:
    declared = {(operation.resource, operation.method) for operation in SDK_SURFACE}
    assert len(declared) == len(SDK_SURFACE)
    assert len({operation.public_name for operation in SDK_SURFACE}) == len(SDK_SURFACE)

    implemented = {
        (resource, name)
        for resource, resource_type in RESOURCE_TYPES.items()
        for name, value in vars(resource_type).items()
        if name == "__call__"
        or (
            not name.startswith("_")
            and name != "__init__"
            and (callable(value) or isinstance(value, (classmethod, staticmethod)))
        )
    }
    assert declared == implemented


def test_many_helpers_are_composed_locally_not_mapped_to_broker_batches() -> None:
    search_many = next(
        operation
        for operation in SDK_SURFACE
        if operation.resource == "search" and operation.method == "many"
    )

    assert search_many.transport_method is None
    assert "search.query_many" not in SDK_TRANSPORT_METHODS

    fetch_many = next(
        operation
        for operation in SDK_SURFACE
        if operation.resource == "content" and operation.method == "fetch_many"
    )
    assert fetch_many.transport_method is None
    assert "content.fetch_many" not in SDK_TRANSPORT_METHODS

    extract_many = next(
        operation
        for operation in SDK_SURFACE
        if operation.resource == "llm" and operation.method == "extract_many"
    )
    assert extract_many.transport_method is None
    assert "llm.extract_many" not in SDK_TRANSPORT_METHODS


def test_surface_manifest_keeps_model_core_small() -> None:
    model_core = [operation for operation in SDK_SURFACE if operation.model_core]
    assert len(SDK_SURFACE) == 21
    assert len([item for item in SDK_SURFACE if item.tier is not SurfaceTier.INTERNAL]) == 20
    assert len(model_core) == 11
    assert all(operation.tier in {SurfaceTier.CORE, SurfaceTier.HELPER} for operation in model_core)
    assert any(operation.public_name == "sdk.content.fetch" for operation in model_core)
    assert any(operation.public_name == "sdk.content.fetch_many" for operation in model_core)
    assert any(operation.public_name == "sdk.capabilities" for operation in model_core)
    assert any(operation.public_name == "sdk.llm.extract_many" for operation in model_core)
    assert all(operation.resource != "session" for operation in SDK_SURFACE)
    assert not hasattr(ContentResource, "snippets")
    assert hasattr(ContentResource, "grep")
    assert not hasattr(ContentResource, "grep_report")


def test_public_resources_and_operations_have_bounded_runtime_docs() -> None:
    for resource, resource_type in RESOURCE_TYPES.items():
        resource_doc = inspect.getdoc(resource_type)
        assert resource_doc, f"sdk.{resource} has no runtime documentation"
        assert len(resource_doc) <= 800, f"sdk.{resource} documentation is too large"

    for operation in SDK_SURFACE:
        if operation.tier is SurfaceTier.INTERNAL:
            continue
        operation_doc = inspect.getdoc(
            getattr(RESOURCE_TYPES[operation.resource], operation.method)
        )
        assert operation_doc, f"{operation.public_name} has no runtime documentation"
        assert 80 <= len(operation_doc) <= 1_600, (
            f"{operation.public_name} documentation must be useful without flooding stdout"
        )


def test_sdk_entrypoint_doc_lists_runtime_namespaces_without_initializing() -> None:
    assert opensac_sdk.sdk.__doc__ is not None
    assert "search" in opensac_sdk.sdk.__doc__
    assert "state" in opensac_sdk.sdk.__doc__
    assert "output" not in opensac_sdk.sdk.__doc__


def test_lazy_sdk_exposes_resource_and_method_docs_without_a_broker_call() -> None:
    opensac_sdk.sdk.close()
    try:
        with patch.dict(
            "os.environ",
            {
                "OPENSAC_BROKER_SOCKET": "/tmp/doc-probe.sock",
                "OPENSAC_SESSION_TOKEN": "doc-probe",
                "OPENSAC_WORKSPACE": "/tmp/doc-probe-workspace",
            },
        ):
            search_doc = opensac_sdk.sdk.search.__doc__ or ""
            assert "sdk.search(query" in search_doc
            assert "canonical web URL" in search_doc
            many_doc = " ".join((opensac_sdk.sdk.search.many.__doc__ or "").split())
            assert "status" in many_doc
            assert '"success"' in many_doc
            assert '"failure"' in many_doc
            assert "structured failure details" in many_doc
            assert opensac_sdk.sdk.capabilities.__doc__ is not None
            assert not hasattr(opensac_sdk.sdk, "output")
            assert not hasattr(opensac_sdk.sdk, "session")
    finally:
        opensac_sdk.sdk.close()


class FakeTransport:
    def __init__(self) -> None:
        self.calls = []

    def call(self, method, params):
        self.calls.append((method, params))
        return wrap(
            [
                {
                    "source": "source_1",
                    "backend": "web",
                    "title": "Title",
                    "domain": "example.com",
                    "date": None,
                    "snippet": "text",
                    "rank": 1,
                }
            ]
        )


def test_unix_transport_reuses_one_http_client_for_all_calls() -> None:
    response = httpx.Response(
        200,
        request=httpx.Request("POST", "http://opensac/v1/call"),
        json={
            "capability_contract": 15,
            "ok": True,
            "result": {"value": 1},
            "error": None,
        },
    )

    class FakeClient:
        def __init__(self) -> None:
            self.posts = 0
            self.closed = 0
            self.headers = []

        def post(self, *_args, **kwargs):
            self.posts += 1
            self.headers.append(kwargs["headers"])
            return response

        def close(self) -> None:
            self.closed += 1

    fake = FakeClient()
    with patch("opensac_sdk.transport.httpx.Client", return_value=fake) as client_type:
        transport = UnixSocketTransport("/tmp/broker.sock", "token")
        assert transport.call("session.capabilities", {}) == {"value": 1}
        assert transport.call("session.capabilities", {}) == {"value": 1}
        transport.close()

    assert client_type.call_count == 1
    assert client_type.call_args.kwargs["timeout"] is None
    assert fake.posts == 2
    assert fake.closed == 1
    assert all(headers["X-OpenSAC-Capability-Contract"] == "15" for headers in fake.headers)


def test_unix_transport_exposes_typed_broker_errors() -> None:
    response = httpx.Response(
        200,
        request=httpx.Request("POST", "http://opensac/v1/call"),
        json={
            "capability_contract": 15,
            "ok": False,
            "result": None,
            "error": {
                "code": "provider_unavailable",
                "message": "model service is unavailable",
                "retryable": True,
                "attempts": 3,
                "provider_status": 503,
                "retry_after_seconds": 1.5,
                "provider": "jina_reader",
                "component": "document",
                "scope": "provider",
            },
        },
    )

    class FakeClient:
        def post(self, *_args, **_kwargs):
            return response

    with patch("opensac_sdk.transport.httpx.Client", return_value=FakeClient()):
        transport = UnixSocketTransport("/tmp/broker.sock", "token")
        with pytest.raises(BrokerError, match="model service") as raised:
            transport.call("llm.complete", {"prompt": "hello"})

    assert raised.value.code == "provider_unavailable"
    assert raised.value.retryable is True
    assert raised.value.attempts == 3
    assert raised.value.provider_status == 503
    assert raised.value.retry_after_seconds == 1.5
    assert raised.value.provider == "jina_reader"
    assert raised.value.component == "document"
    assert raised.value.scope == "provider"


@pytest.mark.parametrize("reported_contract", [None, 14])
def test_unix_transport_rejects_missing_or_mismatched_capability_contract(
    reported_contract: int | None,
) -> None:
    payload = {"ok": True, "result": {}}
    if reported_contract is not None:
        payload["capability_contract"] = reported_contract
    response = httpx.Response(
        200,
        request=httpx.Request("POST", "http://opensac/v1/call"),
        json=payload,
    )

    class FakeClient:
        def post(self, *_args, **_kwargs):
            return response

    with patch("opensac_sdk.transport.httpx.Client", return_value=FakeClient()):
        transport = UnixSocketTransport("/tmp/broker.sock", "token")
        with pytest.raises(BrokerError, match="Capability contract mismatch") as raised:
            transport.call("session.capabilities", {})

    assert raised.value.code == "capability_contract_mismatch"
    assert raised.value.retryable is False


def test_unix_transport_rejects_invalid_json_as_a_protocol_error() -> None:
    response = httpx.Response(
        200,
        request=httpx.Request("POST", "http://opensac/v1/call"),
        content=b"not-json",
    )

    class FakeClient:
        def post(self, *_args, **_kwargs):
            return response

    with patch("opensac_sdk.transport.httpx.Client", return_value=FakeClient()):
        transport = UnixSocketTransport("/tmp/broker.sock", "token")
        with pytest.raises(BrokerError, match="invalid JSON") as raised:
            transport.call("session.capabilities", {})

    assert raised.value.code == "broker_protocol_error"
    assert raised.value.retryable is False


def test_search_resource_returns_typed_hits() -> None:
    transport = FakeTransport()
    hits = SearchResource(transport)("query", limit=3)
    assert hits[0].source == "source_1"
    assert transport.calls == [
        (
            "search.query",
            {"query": "query", "limit": 3, "offset": 0, "include_domains": None},
        )
    ]


def _search_report(
    results: list[Record | dict[str, object]],
    *,
    failures: list[Record | dict[str, object]] | None = None,
    input_count: int | None = None,
) -> Record:
    normalized_results = []
    for default_index, result in enumerate(results):
        row = dict(result)
        row.setdefault("input_index", default_index)
        normalized_results.append(row)
    normalized_failures = [dict(failure) for failure in failures or []]
    if input_count is None:
        indexes = [row["input_index"] for row in normalized_results]
        indexes.extend(row["input_index"] for row in normalized_failures)
        input_count = max(indexes, default=-1) + 1
    return record(
        {
            "results": normalized_results,
            "failures": normalized_failures,
            "input_count": input_count,
        }
    )


def _search_outcomes(
    results: list[Record | dict[str, object]],
    *,
    failures: list[Record | dict[str, object]] | None = None,
    input_count: int | None = None,
) -> list[Record]:
    report = _search_report(results, failures=failures, input_count=input_count)
    outcomes: list[Record | None] = [None] * report.input_count
    for result in report.results:
        outcomes[result.input_index] = record(
            {
                "query": result.query,
                "status": "success",
                "hits": result.hits,
                "error": None,
            }
        )
    for failure in report.failures:
        outcomes[failure.input_index] = record(
            {
                "query": failure.query,
                "status": "failure",
                "hits": [],
                "error": error_info(failure),
            }
        )
    assert all(outcome is not None for outcome in outcomes)
    return [outcome for outcome in outcomes if outcome is not None]


def test_search_many_returns_input_aligned_outcomes() -> None:
    class ManyTransport:
        def call(self, method, params):
            if method == "session.capabilities":
                return _search_capabilities()
            assert method == "search.query"
            assert params["query"] in {"one", "two"}
            assert params["limit"] == 12
            assert params["offset"] == 4
            assert params["include_domains"] == ["example.com"]
            return []

    outcomes = SearchResource(ManyTransport()).many(
        ["one", "two"],
        limit=12,
        offset=4,
        include_domains=["example.com"],
    )

    assert [dict(outcome) for outcome in outcomes] == [
        {"query": "one", "status": "success", "hits": [], "error": None},
        {"query": "two", "status": "success", "hits": [], "error": None},
    ]


def test_search_many_records_all_failed_warning_without_raising(tmp_path, monkeypatch) -> None:
    output_path = tmp_path / "output.json"
    monkeypatch.setenv("OPENSAC_OUTPUT_PATH", str(output_path))

    class FailedManyTransport:
        def call(self, method, params):
            if method == "session.capabilities":
                return _search_capabilities()
            assert method == "search.query"
            raise BrokerError(
                "Search provider timed out.",
                code="provider_timeout",
                retryable=True,
                attempts=3,
                provider="serper",
                component="search",
                scope="provider",
            )

    search = SearchResource(FailedManyTransport())
    outcomes = search.many(["one", "two"])
    assert search.fuse_rrf(outcomes) == []

    assert [outcome.query for outcome in outcomes] == ["one", "two"]
    assert all(outcome.hits == [] for outcome in outcomes)
    assert all(outcome.status == "failure" for outcome in outcomes)
    assert all(outcome.error.code == "provider_timeout" for outcome in outcomes)
    assert all(outcome.error.message == "Search provider timed out." for outcome in outcomes)
    assert all(outcome.error.retryable is True for outcome in outcomes)
    assert all(outcome.error.attempts == 3 for outcome in outcomes)
    assert all(outcome.error.provider == "serper" for outcome in outcomes)
    warnings = json.loads(output_path.read_text(encoding="utf-8"))["warnings"]
    assert len(warnings) == 1
    warning = warnings[0]
    assert warning["method"] == "search.many"
    assert warning["success_count"] == 0
    assert warning["failure_count"] == 2
    assert [failure["query"] for failure in warning["failures"]] == ["one", "two"]
    assert all(failure["provider"] == "serper" for failure in warning["failures"])


def test_sdk_failure_warnings_are_strictly_bounded(tmp_path, monkeypatch) -> None:
    output_path = tmp_path / "output.json"
    monkeypatch.setenv("OPENSAC_OUTPUT_PATH", str(output_path))

    class FailedManyTransport:
        def call(self, method, params):
            del params
            if method == "session.capabilities":
                return _search_capabilities()
            assert method == "search.query"
            raise BrokerError(
                "x" * 10_000,
                code="provider_timeout",
                retryable=True,
                attempts=3,
            )

    outcomes = SearchResource(FailedManyTransport()).many([f"query-{index}" for index in range(64)])

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    encoded_warnings = json.dumps(
        payload["warnings"], ensure_ascii=False, separators=(",", ":")
    ).encode()
    warning = payload["warnings"][0]
    assert len(encoded_warnings) <= 4_096
    assert len(warning["failures"]) + warning["omitted_failure_count"] == 64
    assert all(outcome.status == "failure" for outcome in outcomes)
    assert all(len(outcome.error.message) <= 1_024 for outcome in outcomes)
    assert all("\n" not in outcome.error.message for outcome in outcomes)


def test_warning_budget_keeps_later_failure_summaries(tmp_path, monkeypatch) -> None:
    output_path = tmp_path / "output.json"
    monkeypatch.setenv("OPENSAC_OUTPUT_PATH", str(output_path))

    class FailedManyTransport:
        def call(self, method, params):
            del params
            if method == "session.capabilities":
                return _search_capabilities()
            assert method == "search.query"
            raise BrokerError(
                "x" * 1_024,
                code="provider_invalid_response" + "x" * 480,
                retryable=False,
                attempts=1,
                provider="p" * 512,
                component="o" * 512,
            )

    search = SearchResource(FailedManyTransport())
    for index in range(16):
        search.many([f"query-{index}-" + "q" * 500])

    warnings = json.loads(output_path.read_text(encoding="utf-8"))["warnings"]
    encoded = json.dumps(warnings, ensure_ascii=False, separators=(",", ":")).encode()
    assert len(warnings) == 16
    assert len(encoded) <= 4_096
    assert all(warning["failure_count"] == 1 for warning in warnings)
    assert all(
        len(warning["failures"]) + warning["omitted_failure_count"] == 1 for warning in warnings
    )


def test_failure_status_is_bounded_single_line_and_not_a_parse_contract() -> None:
    status = failure_status(
        {
            "code": "provider_timeout\x1b[31m",
            "message": "provider\n timed\tout",
            "retryable": True,
            "attempts": 3,
        }
    )

    assert status.startswith("failure[provider_timeout [31m]: provider timed out")
    assert "\x1b" not in status
    assert "\n" not in status
    assert len(status) <= 2_048
    assert failure_status({"code": "unknown", "message": "   "}) == (
        "failure[unknown]: Operation failed"
    )


@pytest.mark.parametrize("retry_after_seconds", [float("nan"), float("inf"), -1.0])
def test_error_info_is_total_and_strict_json_serializable(retry_after_seconds: float) -> None:
    info = error_info(
        {
            "code": "provider_timeout",
            "message": "timed out",
            "retryable": True,
            "retry_after_seconds": retry_after_seconds,
            "scope": "unexpected-scope",
        }
    )

    assert info["retry_after_seconds"] is None
    assert info["scope"] == "unknown"
    json.dumps(info, allow_nan=False)


def test_run_many_bounds_workers_preserves_identity_and_joins_threads() -> None:
    lock = threading.Lock()
    active = 0
    max_active = 0

    def call(index: int) -> int:
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        try:
            time.sleep((4 - index) * 0.01)
            return index * 10
        finally:
            with lock:
                active -= 1

    report = _run_many([0, 1, 2, 3], concurrency=2, call=call)

    assert [
        (result.input_index, result.item, result.value)
        for result in report.outcomes
        if isinstance(result, _ManySuccess)
    ] == [
        (0, 0, 0),
        (1, 1, 10),
        (2, 2, 20),
        (3, 3, 30),
    ]
    assert report.success_count == 4
    assert report.failure_count == 0
    assert max_active == 2
    assert active == 0
    assert not any(thread.name.startswith("opensac-sdk") for thread in threading.enumerate())


def test_run_many_runs_one_item_inline_and_only_captures_broker_errors() -> None:
    caller_thread = threading.get_ident()
    inline = _run_many(
        ["one"],
        concurrency=5,
        call=lambda _item: threading.get_ident(),
    )
    assert isinstance(inline.outcomes[0], _ManySuccess)
    assert inline.outcomes[0].value == caller_thread

    failure = BrokerError(
        "provider failed",
        code="provider_timeout",
        retryable=True,
        attempts=2,
    )
    captured = _run_many(
        ["one"],
        concurrency=1,
        call=lambda _item: (_ for _ in ()).throw(failure),
    )
    assert isinstance(captured.outcomes[0], _ManyFailure)
    assert captured.outcomes[0].error is failure
    assert captured.outcomes[0].input_index == 0
    assert captured.outcomes[0].item == "one"
    assert captured.outcomes[0].info["code"] == "provider_timeout"
    assert captured.success_count == 0
    assert captured.failure_count == 1

    with pytest.raises(ValueError, match="unexpected"):
        _run_many(
            ["one", "two"],
            concurrency=2,
            call=lambda _item: (_ for _ in ()).throw(ValueError("unexpected")),
        )
    assert not any(thread.name.startswith("opensac-sdk") for thread in threading.enumerate())


def test_many_report_promotes_only_all_system_failures() -> None:
    def fail(code: str):
        return lambda _item: (_ for _ in ()).throw(BrokerError(code, code=code, retryable=False))

    report = _run_many(
        ["one", "two"],
        concurrency=2,
        call=fail("broker_transport_error"),
    )
    with pytest.raises(BrokerError) as raised:
        report.raise_for_all_system_failures()
    assert raised.value is report.failures[0].error

    provider_report = _run_many(
        ["one", "two"],
        concurrency=2,
        call=fail("provider_timeout"),
    )
    provider_report.raise_for_all_system_failures()


def _hit(source: str, rank: int, *, backend: str = "local", score: float | None = None):
    return record(
        {
            "source": source,
            "backend": backend,
            "title": source,
            "rank": rank,
            "score": score,
            "retrieval": {
                "mode": "dense",
                "result_mode": "query_aware",
                "score_name": "backend_score",
                "higher_is_better": True,
                "comparable_across_queries": False,
            },
        }
    )


def _search_capabilities(
    *,
    batching: bool = True,
    supports_domains: bool = True,
    max_depth: int | None = None,
    max_queries: int = 64,
    max_concurrency: int = 20,
    max_limit: int = 100,
    max_offset: int = 500,
    max_top_k: int = 600,
) -> Record:
    return record(
        {
            "contracts": {"sandbox": 14, "capability": 15},
            "search": {
                "backend": "test",
                "supports_include_domains": supports_domains,
                "max_depth": max_depth,
                "limits": {
                    "max_queries_per_request": max_queries,
                    "max_query_chars": 4_096,
                    "max_top_k": max_top_k,
                    "max_limit": max_limit,
                    "max_offset": max_offset,
                    "max_concurrency": max_concurrency,
                },
            },
            "mechanisms": {"batching": batching},
        }
    )


def test_search_many_is_bounded_aligned_and_does_not_deduplicate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSAC_OUTPUT_PATH", str(tmp_path / "output.json"))

    class ClientTransport:
        def __init__(self) -> None:
            self.lock = threading.Lock()
            self.query_calls: list[str] = []
            self.active = 0
            self.max_active = 0

        def call(self, method, params):
            if method == "session.capabilities":
                return _search_capabilities()
            assert method == "search.query"
            query = params["query"]
            with self.lock:
                self.query_calls.append(query)
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            try:
                time.sleep({"slow": 0.04, "failed": 0.01, "fast": 0.005}[query])
                if query == "failed":
                    raise BrokerError(
                        "Search provider timed out.",
                        code="provider_timeout",
                        retryable=True,
                        attempts=3,
                        provider="test",
                        component="search",
                        scope="provider",
                    )
                return [_hit(f"doc-{query}", 1)]
            finally:
                with self.lock:
                    self.active -= 1

    transport = ClientTransport()
    outcomes = SearchResource(transport).many(
        ["slow", "failed", "fast", "slow"],
        concurrency=2,
    )

    assert [outcome.query for outcome in outcomes] == ["slow", "failed", "fast", "slow"]
    assert [outcome.status for outcome in outcomes] == [
        "success",
        "failure",
        "success",
        "success",
    ]
    assert outcomes[0].error is None
    assert outcomes[1].hits == []
    assert dict(outcomes[1].error) == {
        "code": "provider_timeout",
        "message": "Search provider timed out.",
        "retryable": True,
        "attempts": 3,
        "provider_status": None,
        "retry_after_seconds": None,
        "provider": "test",
        "component": "search",
        "scope": "provider",
    }
    assert transport.max_active == 2
    assert transport.query_calls.count("slow") == 2
    warning = json.loads((tmp_path / "output.json").read_text())["warnings"][0]
    assert warning["success_count"] == 3
    assert warning["failure_count"] == 1


def test_search_many_checks_manifest_admission_before_fanout() -> None:
    class ManifestTransport:
        def __init__(self, manifest: Record) -> None:
            self.manifest = manifest
            self.calls: list[str] = []

        def call(self, method, params):
            del params
            self.calls.append(method)
            if method == "session.capabilities":
                return self.manifest
            raise AssertionError("search fan-out must not start after failed admission")

    cases = [
        (
            _search_capabilities(batching=False),
            ["one", "two"],
            {},
            "capability_disabled",
        ),
        (_search_capabilities(max_queries=1), ["one", "two"], {}, "invalid_request"),
        (_search_capabilities(max_concurrency=1), ["one"], {"concurrency": 2}, "invalid_request"),
        (
            _search_capabilities(supports_domains=False),
            ["one"],
            {"include_domains": ["example.com"]},
            "invalid_request",
        ),
        (_search_capabilities(max_depth=5), ["one"], {}, "invalid_request"),
    ]
    for manifest, queries, kwargs, code in cases:
        transport = ManifestTransport(manifest)
        with pytest.raises(BrokerError) as raised:
            SearchResource(transport).many(queries, **kwargs)
        assert raised.value.code == code
        assert transport.calls == ["session.capabilities"]


def test_search_many_rejects_malformed_manifest() -> None:
    class MalformedTransport:
        def call(self, method, params):
            del method, params
            return {"search": {}}

    with pytest.raises(BrokerError) as raised:
        SearchResource(MalformedTransport()).many(["one"])
    assert raised.value.code == "broker_protocol_error"


def test_search_many_promotes_only_all_system_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSAC_OUTPUT_PATH", str(tmp_path / "output.json"))

    class FailingTransport:
        def __init__(self, *, code: str, succeed: set[str] | None = None) -> None:
            self.code = code
            self.succeed = succeed or set()

        def call(self, method, params):
            if method == "session.capabilities":
                return _search_capabilities()
            if params["query"] in self.succeed:
                return []
            raise BrokerError(
                f"{self.code} for {params['query']}",
                code=self.code,
                retryable=self.code == "broker_transport_error",
                attempts=1 if self.code.startswith("provider_") else None,
            )

    for code in sorted(_SYSTEM_FAILURE_CODES):
        with pytest.raises(BrokerError) as raised:
            SearchResource(FailingTransport(code=code)).many(["one", "two"])
        assert raised.value.code == code

    class MixedSystemTransport(FailingTransport):
        def call(self, method, params):
            if method == "session.capabilities":
                return _search_capabilities()
            code = "broker_transport_error" if params["query"] == "one" else "broker_protocol_error"
            raise BrokerError(f"{code} for {params['query']}", code=code, retryable=False)

    with pytest.raises(BrokerError) as mixed_system:
        SearchResource(MixedSystemTransport(code="unused")).many(["one", "two"])
    assert mixed_system.value.code == "broker_transport_error"

    mixed = SearchResource(
        FailingTransport(code="broker_transport_error", succeed={"one"}),
    ).many(["one", "two"])
    assert [outcome.status for outcome in mixed] == ["success", "failure"]
    assert mixed[1].error.code == "broker_transport_error"

    provider = SearchResource(FailingTransport(code="provider_timeout")).many(["one", "two"])
    assert [outcome.status for outcome in provider] == ["failure", "failure"]
    assert [outcome.error.code for outcome in provider] == [
        "provider_timeout",
        "provider_timeout",
    ]


def test_search_many_validates_empty_input_without_starting_search() -> None:
    class EmptyTransport:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def call(self, method, params):
            del params
            self.calls.append(method)
            return _search_capabilities()

    transport = EmptyTransport()
    assert SearchResource(transport).many([]) == []
    assert transport.calls == ["session.capabilities"]


def test_search_many_restores_partial_duplicate_queries_to_input_order(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("OPENSAC_OUTPUT_PATH", str(tmp_path / "output.json"))

    class PartialManyTransport:
        def call(self, method, params):
            if method == "session.capabilities":
                return _search_capabilities()
            assert method == "search.query"
            if params["query"] == "failed":
                raise BrokerError(
                    "timed out",
                    code="provider_timeout",
                    retryable=True,
                    attempts=2,
                )
            return [_hit("doc-same", 1)]

    search = SearchResource(PartialManyTransport())
    outcomes = search.many(["same", "failed", "same"])

    assert [outcome.query for outcome in outcomes] == ["same", "failed", "same"]
    assert [outcome.status == "success" for outcome in outcomes] == [True, False, True]
    assert [hit.source for outcome in outcomes for hit in outcome.hits] == [
        "doc-same",
        "doc-same",
    ]
    fused = search.fuse_rrf(outcomes)
    assert [row.input_index for row in fused[0].provenance] == [0, 2]


def test_search_rrf_fuses_sources_locally_and_preserves_provenance() -> None:
    transport = FakeTransport()
    search = SearchResource(transport)
    report = _search_outcomes(
        [
            record(
                {
                    "query": "alpha",
                    "hits": [_hit("a", 1, score=0.9), _hit("a", 3), _hit("b", 2)],
                }
            ),
            record({"query": "beta", "hits": [_hit("b", 1), _hit("a", 2)]}),
        ],
        failures=[
            {
                "input_index": 2,
                "query": "failed",
                "code": "provider_timeout",
                "message": "Provider request timed out",
                "retryable": True,
                "attempts": 1,
            }
        ],
        input_count=3,
    )

    result = search.fuse_rrf(report, weights=[1, 2, 1])

    assert transport.calls == []
    assert [candidate.source for candidate in result] == ["b", "a"]
    assert [candidate.fused_rank for candidate in result] == [1, 2]
    assert report[2].status == "failure"
    assert report[2].error.code == "provider_timeout"

    candidate_a = result[1]
    assert isinstance(candidate_a, Record)
    assert candidate_a.rank == 1
    assert candidate_a.raw_fused_score == candidate_a.fused_score
    assert candidate_a.domain_weight == 1.0
    assert len(candidate_a.provenance) == 2
    assert dict(candidate_a.provenance[0]) == {
        "input_index": 0,
        "query": "alpha",
        "backend": "local",
        "rank": 1,
        "score": 0.9,
    }


def test_search_rrf_has_stable_ties_limit_and_empty_input() -> None:
    search = SearchResource(FakeTransport())
    tied = search.fuse_rrf(
        _search_outcomes(
            [
                record({"query": "first", "hits": [_hit("z", 1)]}),
                record({"query": "second", "hits": [_hit("a", 1)]}),
            ]
        ),
        limit=1,
    )
    assert [candidate.source for candidate in tied] == ["z"]

    empty = search.fuse_rrf(_search_outcomes([]))
    assert empty == []

    zero_limit = search.fuse_rrf(
        _search_outcomes([record({"query": "one", "hits": [_hit("a", 1)]})]),
        limit=0,
    )
    assert zero_limit == []


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"weights": [1]}, "align"),
        ({"weights": [0, 0]}, "greater than zero"),
        ({"weights": [1, -1]}, "non-negative"),
        ({"weights": [1, float("inf")]}, "finite"),
        ({"k": -1}, "non-negative"),
        ({"limit": -1}, "non-negative"),
    ],
)
def test_search_rrf_rejects_invalid_options(kwargs, message) -> None:
    report = _search_outcomes(
        [
            record({"query": "one", "hits": [_hit("a", 1)]}),
            record({"query": "two", "hits": [_hit("b", 1)]}),
        ]
    )
    with pytest.raises(ValueError, match=message):
        SearchResource(FakeTransport()).fuse_rrf(report, **kwargs)


@pytest.mark.parametrize(
    ("report", "message"),
    [
        ({"results": []}, "list returned by search.many"),
        ([42], "mapping"),
        ([{"query": "one", "status": "success", "hits": "invalid"}], "hits"),
    ],
)
def test_search_rrf_rejects_invalid_outcome_shapes(report, message) -> None:
    with pytest.raises(ValueError, match=message):
        SearchResource(FakeTransport()).fuse_rrf(report)


def test_search_rrf_refuses_non_positive_source_rank() -> None:
    with pytest.raises(ValueError, match="rank"):
        SearchResource(FakeTransport()).fuse_rrf(
            _search_outcomes([record({"query": "bad", "hits": [_hit("a", 0)]})])
        )


def test_search_rrf_applies_domain_policy_before_limit() -> None:
    search = SearchResource(FakeTransport())
    report = _search_outcomes(
        [
            record(
                {
                    "query": "one",
                    "hits": [
                        _hit("https://www.instagram.com/a", 1, backend="web"),
                        _hit("https://noise.example/a", 2, backend="web"),
                        _hit("https://noise.example/b", 3, backend="web"),
                        _hit("https://useful.example/a", 4, backend="web"),
                        _hit("local-document", 5),
                    ],
                }
            )
        ]
    )

    result = search.fuse_rrf(
        report,
        exclude_domains=["Instagram.COM."],
        domain_weights={"noise.example": 0.1},
        max_per_domain=1,
        limit=3,
    )

    assert [candidate.source for candidate in result] == [
        "https://useful.example/a",
        "local-document",
        "https://noise.example/a",
    ]
    assert [candidate.fused_rank for candidate in result] == [1, 2, 3]
    assert result[0].raw_fused_score == pytest.approx(1 / 64)
    assert result[0].domain_weight == 1.0
    assert result[2].fused_score == pytest.approx(result[2].raw_fused_score * 0.1)


def test_search_rrf_uses_most_specific_domain_weight() -> None:
    result = SearchResource(FakeTransport()).fuse_rrf(
        _search_outcomes(
            [
                record(
                    {
                        "query": "one",
                        "hits": [
                            _hit("https://docs.example.com/a", 1, backend="web"),
                            _hit("https://blog.example.com/a", 2, backend="web"),
                        ],
                    }
                )
            ]
        ),
        domain_weights={"example.com": 0.25, "docs.example.com": 2.0},
    )

    assert [candidate.source for candidate in result] == [
        "https://docs.example.com/a",
        "https://blog.example.com/a",
    ]
    assert [candidate.domain_weight for candidate in result] == [2.0, 0.25]


def test_search_rrf_domain_policy_ignores_non_web_and_malformed_sources() -> None:
    result = SearchResource(FakeTransport()).fuse_rrf(
        _search_outcomes(
            [
                record(
                    {
                        "query": "one",
                        "hits": [_hit("local-document", 1), _hit("https://[invalid", 2)],
                    }
                )
            ]
        ),
        exclude_domains=["example.com"],
        max_per_domain=1,
    )

    assert [candidate.source for candidate in result] == ["local-document", "https://[invalid"]
    assert [candidate.domain_weight for candidate in result] == [1.0, 1.0]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"exclude_domains": "example.com"}, "exclude_domains"),
        ({"exclude_domains": ["https://example.com"]}, "domain names"),
        ({"domain_weights": {"example.com": 0}}, "positive finite"),
        ({"domain_weights": {"example.com": float("inf")}}, "positive finite"),
        ({"domain_weights": {"Example.com": 1, "example.com.": 2}}, "duplicate"),
        ({"max_per_domain": 0}, "positive integer"),
    ],
)
def test_search_rrf_rejects_invalid_domain_policy(kwargs, message) -> None:
    report = _search_outcomes([record({"query": "one", "hits": [_hit("a", 1)]})])
    with pytest.raises(ValueError, match=message):
        SearchResource(FakeTransport()).fuse_rrf(report, **kwargs)


def test_search_rrf_ignores_separate_failures() -> None:
    failure = record(
        {
            "input_index": 0,
            "query": "limited",
            "code": "provider_rate_limited",
            "message": "Provider rate limit was exhausted",
            "retryable": True,
            "attempts": 3,
            "provider_status": 429,
            "retry_after_seconds": 2.0,
        }
    )
    report = _search_outcomes([], failures=[failure], input_count=1)

    result = SearchResource(FakeTransport()).fuse_rrf(report)

    assert result == []
    assert report[0].status == "failure"
    assert report[0].error.code == "provider_rate_limited"


def test_content_grep_returns_matches_and_source_aligned_status(tmp_path, monkeypatch) -> None:
    output_path = tmp_path / "output.json"
    monkeypatch.setenv("OPENSAC_OUTPUT_PATH", str(output_path))

    class GrepTransport:
        def __init__(self) -> None:
            self.calls = []

        def call(self, method, params):
            self.calls.append((method, params))
            return record(
                {
                    "pattern": "target",
                    "mode": "literal",
                    "case_sensitive": True,
                    "start_line": 2,
                    "context_lines": 2,
                    "limit_per_source": 4,
                    "matches": [
                        {
                            "source": "source_1",
                            "line": 3,
                            "text": "target",
                            "spans": [{"start_character": 0, "end_character": 6}],
                            "input_index": 0,
                        }
                    ],
                    "source_results": [
                        {
                            "input_index": 0,
                            "source": "source_1",
                            "title": "One",
                            "match_count": 1,
                            "scan_complete": True,
                            "next_start_line": None,
                        }
                    ],
                    "failures": [
                        {
                            "input_index": 1,
                            "source": "source_2",
                            "code": "provider_not_found",
                            "message": "Document was not found",
                            "retryable": False,
                            "attempts": 1,
                            "provider_status": 404,
                        }
                    ],
                    "input_count": 2,
                }
            )

    transport = GrepTransport()
    outcomes = ContentResource(transport).grep(
        "target",
        sources=["source_1", "source_2"],
        mode="literal",
        case_sensitive=True,
        start_line=2,
        context_lines=2,
        limit_per_source=4,
    )

    assert len(outcomes) == 2
    success, failed = outcomes
    assert isinstance(success, Record)
    assert dict(success) == {
        "source": "source_1",
        "title": "One",
        "status": "success",
        "matches": [
            {
                "line": 3,
                "text": "target",
                "before": [],
                "after": [],
                "spans": [{"start_character": 0, "end_character": 6}],
            }
        ],
        "next_start_line": None,
    }
    assert success.matches[0].spans[0].end_character == 6
    assert dict(failed) == {
        "source": "source_2",
        "title": None,
        "status": (
            "failure[provider_not_found]: Document was not found; retryable=false; "
            "attempts=1; provider_status=404"
        ),
        "matches": [],
        "next_start_line": None,
    }
    warning = json.loads(output_path.read_text(encoding="utf-8"))["warnings"][0]
    assert warning["method"] == "content.grep"
    assert warning["failures"][0]["code"] == "provider_not_found"
    assert warning["failures"][0]["provider_status"] == 404
    assert transport.calls == [
        (
            "content.grep",
            {
                "sources": ["source_1", "source_2"],
                "pattern": "target",
                "mode": "literal",
                "case_sensitive": True,
                "start_line": 2,
                "context_lines": 2,
                "limit_per_source": 4,
            },
        )
    ]


def test_content_grep_keeps_duplicate_sources_separate_by_input_position() -> None:
    class DuplicateGrepTransport:
        def call(self, method, params):
            assert method == "content.grep"
            return record(
                {
                    "pattern": params["pattern"],
                    "mode": params["mode"],
                    "case_sensitive": params["case_sensitive"],
                    "start_line": params["start_line"],
                    "context_lines": params["context_lines"],
                    "limit_per_source": params["limit_per_source"],
                    "matches": [
                        {
                            "source": "same-source",
                            "title": "Same",
                            "line": 2,
                            "text": "first target",
                            "before": [],
                            "after": [],
                            "spans": [{"start_character": 6, "end_character": 12}],
                            "input_index": 0,
                        },
                        {
                            "source": "same-source",
                            "title": "Same",
                            "line": 8,
                            "text": "second target",
                            "before": [],
                            "after": [],
                            "spans": [{"start_character": 7, "end_character": 13}],
                            "input_index": 1,
                        },
                    ],
                    "source_results": [
                        {
                            "input_index": 1,
                            "source": "same-source",
                            "title": "Same",
                            "match_count": 1,
                            "scan_complete": True,
                            "next_start_line": None,
                        },
                        {
                            "input_index": 0,
                            "source": "same-source",
                            "title": "Same",
                            "match_count": 1,
                            "scan_complete": False,
                            "next_start_line": 3,
                        },
                    ],
                    "failures": [],
                    "input_count": 2,
                }
            )

    outcomes = ContentResource(DuplicateGrepTransport()).grep(
        "target", sources=["same-source", "same-source"], mode="literal"
    )

    assert [outcome.source for outcome in outcomes] == ["same-source", "same-source"]
    assert [outcome.matches[0].line for outcome in outcomes] == [2, 8]
    assert [outcome.next_start_line for outcome in outcomes] == [3, None]


def test_content_fetch_forwards_one_source() -> None:
    class FetchTransport:
        def __init__(self) -> None:
            self.calls = []

        def call(self, method, params):
            self.calls.append((method, params))
            return record({"source": params["source"], "text": "body", "metadata": {}})

    transport = FetchTransport()
    document = ContentResource(transport).fetch("source_1")

    assert document.source == "source_1"
    assert document.text == "body"
    assert transport.calls == [("content.fetch", {"source": "source_1"})]


def test_content_fetch_many_is_bounded_aligned_and_preserves_duplicates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSAC_OUTPUT_PATH", str(tmp_path / "output.json"))

    class FetchManyTransport:
        def __init__(self) -> None:
            self.lock = threading.Lock()
            self.calls: list[str] = []
            self.active = 0
            self.max_active = 0

        def call(self, method, params):
            assert method == "content.fetch"
            source = params["source"]
            with self.lock:
                self.calls.append(source)
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            try:
                time.sleep({"slow": 0.04, "failed": 0.01, "fast": 0.005}[source])
                if source == "failed":
                    raise BrokerError(
                        "Document provider timed out.",
                        code="provider_timeout",
                        retryable=True,
                        attempts=3,
                        provider="test",
                        component="document",
                        scope="provider",
                    )
                return record(
                    {
                        "source": source,
                        "title": source,
                        "date": None,
                        "text": f"body-{source}",
                        "metadata": {},
                    }
                )
            finally:
                with self.lock:
                    self.active -= 1

    transport = FetchManyTransport()
    outcomes = ContentResource(transport).fetch_many(
        [" slow ", "failed", "fast", "slow"],
        concurrency=2,
    )

    assert [outcome.source for outcome in outcomes] == ["slow", "failed", "fast", "slow"]
    assert [outcome.status for outcome in outcomes] == [
        "success",
        "failure",
        "success",
        "success",
    ]
    assert outcomes[0].document.text == "body-slow"
    assert outcomes[0].error is None
    assert outcomes[1].document is None
    assert dict(outcomes[1].error) == {
        "code": "provider_timeout",
        "message": "Document provider timed out.",
        "retryable": True,
        "attempts": 3,
        "provider_status": None,
        "retry_after_seconds": None,
        "provider": "test",
        "component": "document",
        "scope": "provider",
    }
    assert transport.max_active == 2
    assert transport.calls.count("slow") == 2
    assert transport.active == 0
    assert not any(thread.name.startswith("opensac-sdk") for thread in threading.enumerate())

    warning = json.loads((tmp_path / "output.json").read_text())["warnings"][0]
    assert warning["method"] == "content.fetch_many"
    assert warning["success_count"] == 3
    assert warning["failure_count"] == 1
    assert warning["failures"][0]["input_index"] == 1
    assert warning["failures"][0]["source"] == "failed"


def test_content_fetch_many_returns_provider_failures_and_promotes_system_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSAC_OUTPUT_PATH", str(tmp_path / "output.json"))

    class FailingTransport:
        def __init__(self, *, code: str, succeed: set[str] | None = None) -> None:
            self.code = code
            self.succeed = succeed or set()

        def call(self, method, params):
            assert method == "content.fetch"
            source = params["source"]
            if source in self.succeed:
                return record({"source": source, "text": "body", "metadata": {}})
            raise BrokerError(
                f"{self.code} for {source}",
                code=self.code,
                retryable=self.code == "broker_transport_error",
                attempts=1 if self.code.startswith("provider_") else None,
            )

    provider = ContentResource(FailingTransport(code="provider_timeout")).fetch_many(["one", "two"])
    assert [outcome.status for outcome in provider] == ["failure", "failure"]
    assert [outcome.error.code for outcome in provider] == [
        "provider_timeout",
        "provider_timeout",
    ]

    for code in sorted(_SYSTEM_FAILURE_CODES):
        with pytest.raises(BrokerError) as raised:
            ContentResource(FailingTransport(code=code)).fetch_many(["one", "two"])
        assert raised.value.code == code

    class MixedSystemTransport(FailingTransport):
        def call(self, method, params):
            assert method == "content.fetch"
            code = (
                "broker_transport_error" if params["source"] == "one" else "broker_protocol_error"
            )
            raise BrokerError(f"{code} for {params['source']}", code=code, retryable=False)

    with pytest.raises(BrokerError) as mixed_system:
        ContentResource(MixedSystemTransport(code="unused")).fetch_many(["one", "two"])
    assert mixed_system.value.code == "broker_transport_error"

    mixed = ContentResource(
        FailingTransport(code="broker_transport_error", succeed={"one"})
    ).fetch_many(["one", "two"])
    assert [outcome.status for outcome in mixed] == ["success", "failure"]
    assert mixed[1].error.code == "broker_transport_error"


def test_content_fetch_many_validates_before_fanout_and_handles_empty_input() -> None:
    class RecordingTransport:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        def call(self, method, params):
            self.calls.append((method, params))
            return record({"source": params["source"], "text": "body", "metadata": {}})

    transport = RecordingTransport()
    content = ContentResource(transport)

    assert content.fetch_many([]) == []
    with pytest.raises(ValueError, match="source at input index 1 must not be empty"):
        content.fetch_many(["valid", "  "])
    with pytest.raises(ValueError, match="sources must be a list"):
        content.fetch_many(("valid",))
    with pytest.raises(ValueError, match="concurrency must be at least 1"):
        content.fetch_many([], concurrency=0)

    assert transport.calls == []


def test_content_fetch_many_propagates_unexpected_exceptions_and_joins_workers() -> None:
    class InvalidTransport:
        def call(self, method, params):
            assert method == "content.fetch"
            if params["source"] == "invalid":
                raise ValueError("unexpected response")
            time.sleep(0.01)
            return record({"source": params["source"], "text": "body", "metadata": {}})

    with pytest.raises(ValueError, match="unexpected response"):
        ContentResource(InvalidTransport()).fetch_many(["one", "invalid", "two"])
    assert not any(thread.name.startswith("opensac-sdk") for thread in threading.enumerate())


def test_content_fetch_many_failure_warnings_are_strictly_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_path = tmp_path / "output.json"
    monkeypatch.setenv("OPENSAC_OUTPUT_PATH", str(output_path))

    class FailedTransport:
        def call(self, method, params):
            del params
            assert method == "content.fetch"
            raise BrokerError(
                "x" * 10_000,
                code="provider_timeout",
                retryable=True,
                attempts=3,
            )

    outcomes = ContentResource(FailedTransport()).fetch_many(
        [f"source-{index}" for index in range(64)]
    )

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    encoded_warnings = json.dumps(
        payload["warnings"], ensure_ascii=False, separators=(",", ":")
    ).encode()
    warning = payload["warnings"][0]
    assert len(encoded_warnings) <= 4_096
    assert len(warning["failures"]) + warning["omitted_failure_count"] == 64
    assert all(outcome.status == "failure" for outcome in outcomes)
    assert all(len(outcome.error.message) <= 1_024 for outcome in outcomes)


def test_content_read_accepts_one_source_and_returns_one_record() -> None:
    class ReadTransport:
        def __init__(self) -> None:
            self.calls = []

        def call(self, method, params):
            self.calls.append((method, params))
            return record({"source": params["source"], "text": "body"})

    transport = ReadTransport()
    row = ContentResource(transport).read(
        "source_1",
        start_line=3,
        start_character=2,
        line_count=4,
        max_chars=50,
    )

    assert row.source == "source_1"
    assert row.text == "body"
    assert transport.calls == [
        (
            "content.read",
            {
                "source": "source_1",
                "start_line": 3,
                "start_character": 2,
                "line_count": 4,
                "max_chars": 50,
            },
        )
    ]


def test_content_read_raises_top_level_failure() -> None:
    class FailedReadTransport:
        def call(self, method, params):
            assert method == "content.read"
            raise BrokerError(
                "Document could not be fetched.",
                code="provider_not_found",
                retryable=False,
                attempts=1,
                provider="jina",
                component="document",
                scope="resource",
            )

    with pytest.raises(BrokerError) as raised:
        ContentResource(FailedReadTransport()).read("https://example.com/missing")

    assert raised.value.code == "provider_not_found"
    assert raised.value.provider == "jina"


def test_content_passages_returns_nested_records() -> None:
    class PassageTransport:
        def __init__(self) -> None:
            self.calls = []

        def call(self, method, params):
            self.calls.append((method, params))
            return record(
                {
                    "query": "revenue singapore",
                    "passages": [
                        {
                            "source": "source_1",
                            "title": "Annual report",
                            "date": "2024",
                            "text": "Singapore revenue was 42 million dollars.",
                            "coordinates": {
                                "start_line": 7,
                                "start_character": 0,
                                "end_line": 7,
                                "end_character": 41,
                            },
                            "rank": 1,
                            "score": 3.5,
                            "ranker": "lexical:bm25",
                        }
                    ],
                    "failures": [],
                    "input_count": 2,
                    "unique_source_count": 1,
                }
            )

    transport = PassageTransport()
    report = ContentResource(transport).passages(
        "revenue singapore",
        sources=["source_1", "source_1"],
        limit=5,
        limit_per_source=2,
    )

    assert isinstance(report, Record)
    assert isinstance(report.passages[0], Record)
    assert isinstance(report.passages[0].coordinates, Record)
    assert "locator" not in report.passages[0]
    assert report.input_count == 2
    assert report.unique_source_count == 1
    assert transport.calls == [
        (
            "content.passages",
            {
                "query": "revenue singapore",
                "sources": ["source_1", "source_1"],
                "limit": 5,
                "limit_per_source": 2,
            },
        )
    ]


def test_content_rejects_record_inputs_before_transport() -> None:
    transport = FakeTransport()
    content = ContentResource(transport)

    with pytest.raises(ValueError, match="source must be a string"):
        content.fetch(record({"source": "https://example.com"}))
    with pytest.raises(ValueError, match="source must not be empty"):
        content.read("  ")

    assert transport.calls == []


def test_sdk_rejects_invalid_public_parameters_before_transport() -> None:
    transport = FakeTransport()

    with pytest.raises(ValueError, match="query must be a string"):
        SearchResource(transport)(42)
    with pytest.raises(ValueError, match="limit must be an integer"):
        SearchResource(transport)("query", limit=True)
    with pytest.raises(ValueError, match="start_line must be at least 1"):
        ContentResource(transport).read("source_1", start_line=0)
    with pytest.raises(ValueError, match="mode"):
        ContentResource(transport).grep("target", sources=["source_1"], mode="auto")
    with pytest.raises(ValueError, match="temperature"):
        LLMResource(transport).complete("prompt", temperature=float("nan"))
    with pytest.raises(ValueError, match="repair_attempts"):
        LLMResource(transport).extract({}, instruction="x", schema={}, repair_attempts=-1)

    assert transport.calls == []


def test_capabilities_resource_uses_session_broker_operation() -> None:
    class SessionTransport:
        def __init__(self) -> None:
            self.calls = []

        def call(self, method, params):
            self.calls.append((method, params))
            return record({"contracts": {"sandbox": 14, "capability": 15}})

    transport = SessionTransport()
    capabilities = CapabilitiesResource(transport)()

    assert capabilities.contracts.sandbox == 14
    assert capabilities.contracts.capability == 15
    assert transport.calls == [("session.capabilities", {})]


class ExtractionTransport:
    def __init__(self, result):
        self.result = wrap(result)
        self.calls = []

    def call(self, method, params):
        self.calls.append((method, params))
        return self.result


def test_extract_returns_validated_object_and_forwards_repair() -> None:
    transport = ExtractionTransport({"matches": True})

    result = LLMResource(transport).extract(
        {"text": "yes"},
        instruction="Classify the item",
        schema={
            "type": "object",
            "properties": {"matches": {"type": "boolean"}},
            "required": ["matches"],
        },
        max_tokens=64,
        repair_attempts=2,
    )

    assert isinstance(result, Record)
    assert result.matches is True
    assert transport.calls[0] == (
        "llm.extract",
        {
            "item": {"text": "yes"},
            "instruction": "Classify the item",
            "schema": {
                "type": "object",
                "properties": {"matches": {"type": "boolean"}},
                "required": ["matches"],
            },
            "repair_attempts": 2,
            "max_tokens": 64,
        },
    )


def test_extract_rejects_non_json_values_before_transport() -> None:
    transport = ExtractionTransport({})
    llm = LLMResource(transport)

    with pytest.raises(ValueError, match="schema must contain only strict JSON values"):
        llm.extract({}, instruction="x", schema={"matches": bool})
    with pytest.raises(ValueError, match="item must contain only strict JSON values"):
        llm.extract({"bad": float("nan")}, instruction="x", schema={})
    with pytest.raises(ValueError, match="repair_attempts"):
        llm.extract({}, instruction="x", schema={}, repair_attempts=-1)

    assert transport.calls == []


def test_extract_many_is_bounded_aligned_and_does_not_echo_items(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_path = tmp_path / "output.json"
    monkeypatch.setenv("OPENSAC_OUTPUT_PATH", str(output_path))

    class ExtractManyTransport:
        def __init__(self) -> None:
            self.lock = threading.Lock()
            self.calls: list[dict] = []
            self.active = 0
            self.max_active = 0

        def call(self, method, params):
            assert method == "llm.extract"
            item = params["item"]
            with self.lock:
                self.calls.append(params)
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            try:
                time.sleep({"slow": 0.04, "failed": 0.01, "fast": 0.005}[item["id"]])
                if item["id"] == "failed":
                    raise BrokerError(
                        "Output did not match the schema.",
                        code="schema_mismatch",
                        retryable=False,
                        attempts=2,
                        provider="test",
                        component="llm",
                        scope="resource",
                    )
                return record({"label": item["id"]})
            finally:
                with self.lock:
                    self.active -= 1

    schema = {
        "type": "object",
        "properties": {"label": {"type": "string"}},
        "required": ["label"],
    }
    transport = ExtractManyTransport()
    outcomes = LLMResource(transport).extract_many(
        [{"id": "slow"}, {"id": "failed"}, {"id": "fast"}, {"id": "slow"}],
        instruction="Classify each item",
        schema=schema,
        concurrency=2,
        max_tokens=64,
        repair_attempts=1,
    )

    assert [outcome.input_index for outcome in outcomes] == [0, 1, 2, 3]
    assert [outcome.status for outcome in outcomes] == [
        "success",
        "failure",
        "success",
        "success",
    ]
    assert outcomes[0].data.label == "slow"
    assert outcomes[0].error is None
    assert outcomes[1].data is None
    assert outcomes[1].error.code == "schema_mismatch"
    assert all("item" not in outcome for outcome in outcomes)
    assert transport.max_active == 2
    assert transport.active == 0
    assert all(call["instruction"] == "Classify each item" for call in transport.calls)
    assert all(call["schema"] == schema for call in transport.calls)
    assert all(call["max_tokens"] == 64 for call in transport.calls)
    assert all(call["repair_attempts"] == 1 for call in transport.calls)
    assert not any(thread.name.startswith("opensac-sdk") for thread in threading.enumerate())

    warning = json.loads(output_path.read_text(encoding="utf-8"))["warnings"][0]
    assert warning["method"] == "llm.extract_many"
    assert warning["success_count"] == 3
    assert warning["failure_count"] == 1
    assert warning["failures"][0]["input_index"] == 1
    assert "item" not in warning["failures"][0]


def test_extract_many_validates_every_item_before_fanout_and_handles_empty_input() -> None:
    transport = ExtractionTransport({"label": "ok"})
    llm = LLMResource(transport)

    assert llm.extract_many([], instruction="x", schema={}) == []
    with pytest.raises(ValueError, match="items must be a list"):
        llm.extract_many(({"ok": True},), instruction="x", schema={})
    with pytest.raises(ValueError, match=r"items\[1\] must contain only strict JSON values"):
        llm.extract_many(
            [{"ok": True}, {"bad": float("nan")}],
            instruction="x",
            schema={},
        )
    with pytest.raises(ValueError, match="schema must contain only strict JSON values"):
        llm.extract_many([], instruction="x", schema={"bad": bool})
    with pytest.raises(ValueError, match="concurrency must be at least 1"):
        llm.extract_many([], instruction="x", schema={}, concurrency=0)

    assert transport.calls == []


def test_extract_many_preserves_provider_failures_and_promotes_all_system_failures() -> None:
    class FailingTransport:
        def __init__(self, code: str) -> None:
            self.code = code

        def call(self, method, params):
            assert method == "llm.extract"
            raise BrokerError(
                f"{self.code} for {params['item']['id']}",
                code=self.code,
                retryable=self.code == "provider_timeout",
                attempts=1 if self.code.startswith("provider_") else None,
            )

    kwargs = {"instruction": "x", "schema": {}}
    provider = LLMResource(FailingTransport("provider_timeout")).extract_many(
        [{"id": "one"}, {"id": "two"}],
        **kwargs,
    )
    assert [outcome.status for outcome in provider] == ["failure", "failure"]
    assert [outcome.error.code for outcome in provider] == [
        "provider_timeout",
        "provider_timeout",
    ]

    for code in sorted(_SYSTEM_FAILURE_CODES):
        with pytest.raises(BrokerError) as raised:
            LLMResource(FailingTransport(code)).extract_many(
                [{"id": "one"}, {"id": "two"}],
                **kwargs,
            )
        assert raised.value.code == code

    class MixedSystemTransport(FailingTransport):
        def call(self, method, params):
            assert method == "llm.extract"
            code = (
                "broker_transport_error"
                if params["item"]["id"] == "one"
                else "broker_protocol_error"
            )
            raise BrokerError(code, code=code, retryable=False)

    with pytest.raises(BrokerError) as mixed_system:
        LLMResource(MixedSystemTransport("unused")).extract_many(
            [{"id": "one"}, {"id": "two"}],
            **kwargs,
        )
    assert mixed_system.value.code == "broker_transport_error"


def test_state_round_trip_and_path_confinement(tmp_path) -> None:
    state = StateResource(str(tmp_path))
    state.write_jsonl("nested/data.jsonl", [{"a": 1}, {"a": 2}])
    assert state.read_jsonl("nested/data.jsonl") == [{"a": 1}, {"a": 2}]
    with pytest.raises(ValueError, match="inside"):
        state.write_json("../escape.json", {})


def test_state_rejects_non_json_without_replacing_existing_artifacts(tmp_path) -> None:
    state = StateResource(str(tmp_path))
    state.write_json("value.json", {"ok": "世界"})
    state.write_jsonl("rows.jsonl", [{"ok": 1}])

    with pytest.raises(ValueError, match="strict JSON"):
        state.write_json("value.json", {"bad": {1, 2}})
    with pytest.raises(ValueError, match=r"rows\[1\]"):
        state.write_jsonl("rows.jsonl", [{"ok": 2}, {"bad": float("nan")}])
    with pytest.raises(ValueError, match=r"rows\[0\]"):
        state.append_jsonl("rows.jsonl", [{"bad": float("inf")}])

    assert state.read_json("value.json").ok == "世界"
    assert state.read_jsonl("rows.jsonl") == [{"ok": 1}]


def test_state_accumulates_across_calls_without_rewriting(tmp_path) -> None:
    """Extending a record must not cost the whole file each time.

    The recommended shape saves a candidate pool in one turn and adds evidence
    to it in later ones. With only whole-file writes that is read-everything
    then write-everything, and a program that dies midway loses all of it.
    """
    state = StateResource(str(tmp_path))
    state.append_jsonl("evidence.jsonl", [{"n": 1}])
    state.append_jsonl("evidence.jsonl", [{"n": 2}, {"n": 3}])
    assert state.read_jsonl("evidence.jsonl") == [{"n": 1}, {"n": 2}, {"n": 3}]
    # A whole-file write still replaces, so the two are distinguishable.
    state.write_jsonl("evidence.jsonl", [{"n": 9}])
    assert state.read_jsonl("evidence.jsonl") == [{"n": 9}]


def test_state_upsert_updates_a_pool_across_turns(tmp_path) -> None:
    """The same call on turn 1 and turn 20, which is the whole point.

    A pool kept with ``write_jsonl`` is a snapshot and a pool kept with
    ``append_jsonl`` grows a duplicate per query, so carrying candidates
    forward previously required an ``exists`` guard, a read, a dict merge and
    a write -- in a first turn that has nothing to read. Programs answered that
    by writing ``pool2.jsonl`` instead, which is why this exists.
    """
    state = StateResource(str(tmp_path))
    # No file yet: an absent pool is an empty one, so nothing branches.
    assert state.upsert_jsonl("pool.jsonl", [{"source": "a", "n": 1}, {"source": "b", "n": 1}]) == 2
    # A document a second query returned replaces its row in place rather than
    # adding a second one, and does not move ahead of documents found later.
    assert state.upsert_jsonl("pool.jsonl", [{"source": "c", "n": 1}, {"source": "a", "n": 2}]) == 3
    assert state.read_jsonl("pool.jsonl") == [
        {"source": "a", "n": 2},
        {"source": "b", "n": 1},
        {"source": "c", "n": 1},
    ]
    # Any field can be the identity; ``source`` is only the common one.
    state.upsert_jsonl("docs.jsonl", [{"docid": "7", "seen": 1}], key="docid")
    assert state.upsert_jsonl("docs.jsonl", [{"docid": "7", "seen": 2}], key="docid") == 1


def test_state_upsert_refuses_rows_it_cannot_deduplicate(tmp_path) -> None:
    """Silent duplication is the failure this call exists to prevent.

    Appending a keyless row would make the file grow exactly the way the
    caller reached for ``upsert_jsonl`` to avoid, and nothing downstream would
    show it. The message names the field and the alternative because the
    program has one turn to fix it.
    """
    state = StateResource(str(tmp_path))
    with pytest.raises(ValueError, match="append_jsonl"):
        state.upsert_jsonl("pool.jsonl", [{"title": "no identity here"}])
    assert state.exists("pool.jsonl") is False


def test_state_upsert_keeps_rows_it_did_not_write(tmp_path) -> None:
    """A file written by an earlier, differently-shaped program is not data to drop."""
    state = StateResource(str(tmp_path))
    state.write_jsonl("pool.jsonl", [{"note": "from an earlier turn"}, {"source": "a"}])
    assert state.upsert_jsonl("pool.jsonl", [{"source": "a", "n": 2}]) == 2
    assert state.read_jsonl("pool.jsonl") == [
        {"note": "from an earlier turn"},
        {"source": "a", "n": 2},
    ]


def test_state_upsert_accepts_a_search_hit_directly(tmp_path) -> None:
    """SDK results are ordinary mappings and need no conversion before persistence."""
    state = StateResource(str(tmp_path))
    hit = record(
        {
            "source": "source_1",
            "backend": "local",
            "title": "t",
            "rank": 1,
        }
    )
    assert state.upsert_jsonl("pool.jsonl", [hit]) == 1
    assert state.upsert_jsonl("pool.jsonl", [hit]) == 1
    assert state.read_jsonl("pool.jsonl")[0].source == "source_1"


def test_state_can_be_asked_what_is_there(tmp_path) -> None:
    """A later turn has to tell "saved nothing" from "never ran"."""
    state = StateResource(str(tmp_path))
    assert state.exists("pool.jsonl") is False
    state.write_jsonl("pool.jsonl", [{"a": 1}])
    state.write_json("notes/summary.json", {"b": 2})
    assert state.exists("pool.jsonl") is True

    assert state.list() == ["notes/summary.json", "pool.jsonl"]
    assert state.list("notes/") == ["notes/summary.json"]
    # Runtime internals stay hidden: a program that read or rewrote them would
    # be editing the record of its own execution.
    (tmp_path / ".opensac-output.json").write_text("{}")
    assert ".opensac-output.json" not in state.list()


def test_environment_state_follows_each_persistent_cell_context(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_workspace = tmp_path / "first"
    second_workspace = tmp_path / "second"
    state = StateResource.from_environment()

    monkeypatch.setenv("OPENSAC_WORKSPACE", str(first_workspace))
    state.write_json("state.json", {"cell": 1})

    monkeypatch.setenv("OPENSAC_WORKSPACE", str(second_workspace))
    state.write_json("state.json", {"cell": 2})

    assert json.loads((first_workspace / "state.json").read_text()) == {"cell": 1}
    assert json.loads((second_workspace / "state.json").read_text()) == {"cell": 2}


def test_environment_transport_refreshes_token_and_execution_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[tuple[str, str, str]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        observed.append(
            (
                request.headers["Authorization"],
                request.headers["X-OpenSAC-Execution-ID"],
                request.headers["X-OpenSAC-Capability-Contract"],
            )
        )
        return httpx.Response(
            200,
            json={"capability_contract": 15, "ok": True, "result": {}},
        )

    monkeypatch.setenv("OPENSAC_BROKER_SOCKET", "/tmp/broker.sock")
    monkeypatch.setenv("OPENSAC_SESSION_TOKEN", "token-1")
    transport = UnixSocketTransport.from_environment()
    transport._client = httpx.Client(
        base_url="http://opensac",
        transport=httpx.MockTransport(handle),
    )
    try:
        monkeypatch.setenv("OPENSAC_EXECUTION_ID", "exec-1")
        transport.call("session.capabilities", {})
        monkeypatch.setenv("OPENSAC_SESSION_TOKEN", "token-2")
        monkeypatch.setenv("OPENSAC_EXECUTION_ID", "exec-2")
        transport.call("session.capabilities", {})
    finally:
        transport.close()

    assert observed == [
        ("Bearer token-1", "exec-1", "15"),
        ("Bearer token-2", "exec-2", "15"),
    ]


def test_a_result_answers_to_either_spelling_of_a_field_read() -> None:
    """One type, two access styles -- not two representations tolerating each other.

    Programs reach for `hit["docid"]` because that is what every search API
    they have read returns, and the attribute-only form turns that prior into
    `'SearchHit' object is not subscriptable`, which ends the turn.
    """
    hit = SearchResource(FakeTransport())("query")[0]
    assert hit["source"] == hit.source == "source_1"
    assert hit.get("title") == "Title"
    assert hit.get("nonexistent") is None
    assert hit.get("nonexistent", "fallback") == "fallback"
    assert "source" in hit and "nonexistent" not in hit
    assert dict(hit)["rank"] == 1
    # Neither spelling can reach past the fields, so they cannot disagree.
    with pytest.raises(KeyError):
        hit["nonexistent"]


def test_mapping_access_is_canonical_when_field_names_collide_with_dict_methods() -> None:
    row = record({"items": [1], "values": 2, "get": "field"})

    assert row["items"] == [1]
    assert row["values"] == 2
    assert row["get"] == "field"
    assert callable(row.items)
    assert callable(row.values)
    assert callable(row.get)


def test_a_content_record_carries_source_dates() -> None:
    snippet = record({"source": "source_1", "text": "body", "date": "1994"})
    assert snippet.date == snippet["date"] == "1994"


def test_a_result_written_to_the_workspace_comes_back_readable(tmp_path) -> None:
    """The round trip is where the type is lost, so it must not lose the access.

    JSON cannot carry a Python type, so a hit written in one turn returns as a
    mapping in the next. Programs go on writing `row.source` because that is how
    every other line around it is written.
    """
    state = StateResource(str(tmp_path))
    hit = SearchResource(FakeTransport())("query")[0]

    # Passed straight in: `default=str` would have written the repr instead,
    # and a later turn subscripting that string would get a character.
    state.write_jsonl("pool.jsonl", [hit])
    assert json.loads((tmp_path / "pool.jsonl").read_text())["source"] == "source_1"

    row = state.read_jsonl("pool.jsonl")[0]
    assert row.source == row["source"] == "source_1"
    assert row.rank == 1
    # Still an ordinary dict: it survives json, unpacking and the dict methods.
    assert isinstance(row, dict)
    assert json.dumps(row)
    assert {**row}["title"] == "Title"
    assert sorted(row.keys())[:2] == ["backend", "date"]


def test_a_row_says_what_it_has_when_a_field_is_missing(tmp_path) -> None:
    state = StateResource(str(tmp_path))
    state.write_jsonl("pool.jsonl", [{"source": "source_1", "rank": 2}])
    row = state.read_jsonl("pool.jsonl")[0]
    with pytest.raises(AttributeError, match="rank"):
        _ = row.raank


def test_nesting_reads_the_same_way_at_every_depth(tmp_path) -> None:
    """Eager, so that `row["metadata"].x` and `row.metadata.x` cannot differ."""
    state = StateResource(str(tmp_path))
    state.write_jsonl("pool.jsonl", [{"metadata": {"backend": "local"}, "runs": [{"n": 1}]}])
    row = state.read_jsonl("pool.jsonl")[0]
    assert row.metadata.backend == row["metadata"]["backend"] == "local"
    assert row["metadata"].backend == row.metadata["backend"] == "local"
    assert row.runs[0].n == 1
    # write_json / read_json are the same channel and must not diverge.
    state.write_json("one.json", {"metadata": {"backend": "local"}})
    assert state.read_json("one.json").metadata.backend == "local"
