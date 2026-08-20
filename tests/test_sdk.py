from __future__ import annotations

import importlib
import inspect
import json
from unittest.mock import patch

import httpx
import opensac_sdk
import pytest
from opensac_sdk._record import Record, record, wrap
from opensac_sdk._resources import (
    ContentResource,
    LLMResource,
    OutputResource,
    SearchResource,
    SessionResource,
    StateResource,
)
from opensac_sdk._surface import SDK_SURFACE, SurfaceTier
from opensac_sdk.transport import BrokerError, UnixSocketTransport

RESOURCE_TYPES = {
    "content": ContentResource,
    "llm": LLMResource,
    "output": OutputResource,
    "search": SearchResource,
    "session": SessionResource,
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


def test_surface_manifest_keeps_model_core_small() -> None:
    model_core = [operation for operation in SDK_SURFACE if operation.model_core]
    assert len(model_core) <= 12
    assert all(operation.tier in {SurfaceTier.CORE, SurfaceTier.HELPER} for operation in model_core)
    assert not hasattr(ContentResource, "snippets")
    assert not hasattr(ContentResource, "grep")


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
    assert "output" in opensac_sdk.sdk.__doc__


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
            assert "input query" in (opensac_sdk.sdk.search.many.__doc__ or "")
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
        json={"ok": True, "result": {"value": 1}, "error": None},
    )

    class FakeClient:
        def __init__(self) -> None:
            self.posts = 0
            self.closed = 0

        def post(self, *_args, **_kwargs):
            self.posts += 1
            return response

        def close(self) -> None:
            self.closed += 1

    fake = FakeClient()
    with patch("opensac_sdk.transport.httpx.Client", return_value=fake) as client_type:
        transport = UnixSocketTransport("/tmp/broker.sock", "token")
        assert transport.call("session.usage", {}) == {"value": 1}
        assert transport.call("session.usage", {}) == {"value": 1}
        transport.close()

    assert client_type.call_count == 1
    assert client_type.call_args.kwargs["timeout"] is None
    assert fake.posts == 2
    assert fake.closed == 1


def test_unix_transport_exposes_typed_broker_errors() -> None:
    response = httpx.Response(
        200,
        request=httpx.Request("POST", "http://opensac/v1/call"),
        json={
            "ok": False,
            "result": None,
            "error": {
                "code": "provider_unavailable",
                "message": "model service is unavailable",
                "retryable": True,
                "attempts": 3,
                "provider_status": 503,
                "retry_after_seconds": 1.5,
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
            transport.call("session.usage", {})

    assert raised.value.code == "broker_protocol_error"
    assert raised.value.retryable is False


def test_search_resource_returns_typed_hits() -> None:
    transport = FakeTransport()
    hits = SearchResource(transport)("query", limit=3)
    assert hits[0].source == "source_1"
    assert transport.calls == [
        ("search.query", {"query": "query", "limit": 3, "offset": 0, "domains": None})
    ]


def test_search_many_returns_only_result_semantics() -> None:
    class ManyTransport:
        def call(self, method, params):
            assert method == "search.query_many"
            return wrap([{"query": query, "hits": []} for query in params["queries"]])

    batches = SearchResource(ManyTransport()).many(
        ["one", "two"],
        limit_per_query=12,
        offset=4,
        domains=["example.com"],
    )

    assert [dict(batch) for batch in batches] == [
        {"query": "one", "hits": []},
        {"query": "two", "hits": []},
    ]


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


def test_search_rrf_fuses_sources_locally_and_preserves_provenance() -> None:
    transport = FakeTransport()
    search = SearchResource(transport)
    batches = [
        record(
            {
                "query": "alpha",
                "hits": [_hit("a", 1, score=0.9), _hit("a", 3), _hit("b", 2)],
                "failure": None,
            }
        ),
        record({"query": "beta", "hits": [_hit("b", 1), _hit("a", 2)], "failure": None}),
        record(
            {
                "query": "failed",
                "hits": [],
                "failure": {
                    "code": "provider_timeout",
                    "message": "Provider request timed out",
                    "retryable": True,
                    "attempts": 1,
                },
            }
        ),
    ]

    result = search.fuse_rrf(batches, weights=[1, 2, 1])

    assert transport.calls == []
    assert [candidate.source for candidate in result] == ["b", "a"]
    assert [candidate.fused_rank for candidate in result] == [1, 2]
    assert batches[2].failure is not None

    candidate_a = result[1]
    assert isinstance(candidate_a, Record)
    assert candidate_a.rank == 1
    assert len(candidate_a.provenance) == 2
    assert dict(candidate_a.provenance[0]) == {
        "batch_index": 0,
        "query": "alpha",
        "backend": "local",
        "rank": 1,
        "score": 0.9,
    }


def test_search_rrf_has_stable_ties_limit_and_empty_input() -> None:
    search = SearchResource(FakeTransport())
    tied = search.fuse_rrf(
        [
            record({"query": "first", "hits": [_hit("z", 1)], "failure": None}),
            record({"query": "second", "hits": [_hit("a", 1)], "failure": None}),
        ],
        limit=1,
    )
    assert [candidate.source for candidate in tied] == ["z"]

    empty = search.fuse_rrf([])
    assert empty == []


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
    batches = [
        record({"query": "one", "hits": [_hit("a", 1)], "failure": None}),
        record({"query": "two", "hits": [_hit("b", 1)], "failure": None}),
    ]
    with pytest.raises(ValueError, match=message):
        SearchResource(FakeTransport()).fuse_rrf(batches, **kwargs)


def test_search_rrf_refuses_non_positive_source_rank() -> None:
    with pytest.raises(ValueError, match="rank"):
        SearchResource(FakeTransport()).fuse_rrf(
            [record({"query": "bad", "hits": [_hit("a", 0)], "failure": None})]
        )


def test_search_rrf_skips_failed_batches_without_copying_their_errors() -> None:
    failure = record(
        {
            "code": "provider_rate_limited",
            "message": "Provider rate limit was exhausted",
            "retryable": True,
            "attempts": 3,
            "provider_status": 429,
            "retry_after_seconds": 2.0,
        }
    )
    batch = record({"query": "limited", "hits": [], "failure": failure})

    result = SearchResource(FakeTransport()).fuse_rrf([batch])

    assert result == []
    assert batch.failure == failure


def test_content_grep_report_returns_matches_and_source_aligned_failures() -> None:
    class GrepTransport:
        def __init__(self) -> None:
            self.calls = []

        def call(self, method, params):
            self.calls.append((method, params))
            return record(
                {
                    "matches": [
                        {
                            "source": "source_1",
                            "line": 3,
                            "text": "target",
                            "input_index": 0,
                        }
                    ],
                    "failures": [
                        {
                            "input_index": 1,
                            "source": "source_2",
                            "failure": {
                                "code": "provider_not_found",
                                "message": "Document was not found",
                                "retryable": False,
                                "attempts": 1,
                                "provider_status": 404,
                            },
                        }
                    ],
                    "input_count": 2,
                }
            )

    transport = GrepTransport()
    report = ContentResource(transport).grep_report(
        ["source_1", "source_2"],
        "target",
        context=2,
        max_matches_per_source=4,
    )

    assert isinstance(report, Record)
    assert report.matches[0].source == "source_1"
    assert report.matches[0].line == 3
    assert report.failures[0].input_index == 1
    assert report.failures[0].failure.code == "provider_not_found"
    assert report.failures[0].failure.provider_status == 404
    assert report.input_count == 2
    assert transport.calls == [
        (
            "content.grep_report",
            {
                "sources": ["source_1", "source_2"],
                "pattern": "target",
                "context": 2,
                "max_matches_per_source": 4,
            },
        )
    ]


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
        ["source_1", "source_1"],
        limit=5,
        max_per_source=2,
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
                "max_per_source": 2,
            },
        )
    ]


def test_content_rejects_record_inputs_before_transport() -> None:
    transport = FakeTransport()
    content = ContentResource(transport)

    with pytest.raises(ValueError, match="input index 0 must be a string"):
        content.get_many([record({"source": "https://example.com"})])
    with pytest.raises(ValueError, match="input index 0 must not be empty"):
        content.read(["  "])

    assert transport.calls == []


class ExtractionTransport:
    def __init__(self, result):
        self.result = wrap(result)
        self.calls = []

    def call(self, method, params):
        self.calls.append((method, params))
        return self.result


def test_extract_many_returns_aligned_records_and_forwards_repair() -> None:
    transport = ExtractionTransport(
        [
            {"index": 0, "data": {"matches": True}, "error": None, "attempts": 1},
            {
                "index": 1,
                "data": None,
                "error": {
                    "code": "schema_mismatch",
                    "message": "matches is required",
                    "retryable": False,
                },
                "attempts": 2,
            },
        ]
    )

    results = LLMResource(transport).extract_many(
        [{"text": "yes"}, {"text": "unknown"}],
        instruction="Classify each item",
        schema={
            "type": "object",
            "properties": {"matches": {"type": "boolean"}},
            "required": ["matches"],
        },
        max_tokens=64,
        repair_attempts=1,
    )

    assert isinstance(results[0], Record)
    assert results[0].data.matches is True
    assert results[1].error.code == "schema_mismatch"
    assert results[1].error.message == "matches is required"
    assert transport.calls[0] == (
        "llm.extract_many",
        {
            "items": [{"text": "yes"}, {"text": "unknown"}],
            "instruction": "Classify each item",
            "schema": {
                "type": "object",
                "properties": {"matches": {"type": "boolean"}},
                "required": ["matches"],
            },
            "concurrency": 4,
            "repair_attempts": 1,
            "max_tokens": 64,
        },
    )


def test_extract_many_rejects_non_json_values_before_transport() -> None:
    transport = ExtractionTransport([])
    llm = LLMResource(transport)

    with pytest.raises(ValueError, match="schema must be JSON serializable"):
        llm.extract_many([], instruction="x", schema={"matches": bool})
    with pytest.raises(ValueError, match=r"items\[1\] must be JSON serializable"):
        llm.extract_many([{"ok": 1}, {"bad": float("nan")}], instruction="x", schema={})
    with pytest.raises(ValueError, match="repair_attempts"):
        llm.extract_many([], instruction="x", schema={}, repair_attempts=2)

    assert transport.calls == []


def test_state_round_trip_and_path_confinement(tmp_path) -> None:
    state = StateResource(str(tmp_path))
    state.write_jsonl("nested/data.jsonl", [{"a": 1}, {"a": 2}])
    assert state.read_jsonl("nested/data.jsonl") == [{"a": 1}, {"a": 2}]
    with pytest.raises(ValueError, match="inside"):
        state.write_json("../escape.json", {})


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


def test_state_merge_upserts_a_pool_across_turns(tmp_path) -> None:
    """The same call on turn 1 and turn 20, which is the whole point.

    A pool kept with ``write_jsonl`` is a snapshot and a pool kept with
    ``append_jsonl`` grows a duplicate per query, so carrying candidates
    forward previously required an ``exists`` guard, a read, a dict merge and
    a write -- in a first turn that has nothing to read. Programs answered that
    by writing ``pool2.jsonl`` instead, which is why this exists.
    """
    state = StateResource(str(tmp_path))
    # No file yet: an absent pool is an empty one, so nothing branches.
    assert state.merge_jsonl("pool.jsonl", [{"source": "a", "n": 1}, {"source": "b", "n": 1}]) == 2
    # A document a second query returned replaces its row in place rather than
    # adding a second one, and does not move ahead of documents found later.
    assert state.merge_jsonl("pool.jsonl", [{"source": "c", "n": 1}, {"source": "a", "n": 2}]) == 3
    assert state.read_jsonl("pool.jsonl") == [
        {"source": "a", "n": 2},
        {"source": "b", "n": 1},
        {"source": "c", "n": 1},
    ]
    # Any field can be the identity; ``source`` is only the common one.
    state.merge_jsonl("docs.jsonl", [{"docid": "7", "seen": 1}], key="docid")
    assert state.merge_jsonl("docs.jsonl", [{"docid": "7", "seen": 2}], key="docid") == 1


def test_state_merge_refuses_rows_it_cannot_deduplicate(tmp_path) -> None:
    """Silent duplication is the failure this call exists to prevent.

    Appending a keyless row would make the file grow exactly the way the
    caller reached for ``merge_jsonl`` to avoid, and nothing downstream would
    show it. The message names the field and the alternative because the
    program has one turn to fix it.
    """
    state = StateResource(str(tmp_path))
    with pytest.raises(ValueError, match="append_jsonl"):
        state.merge_jsonl("pool.jsonl", [{"title": "no identity here"}])
    assert state.exists("pool.jsonl") is False


def test_state_merge_keeps_rows_it_did_not_write(tmp_path) -> None:
    """A file written by an earlier, differently-shaped program is not data to drop."""
    state = StateResource(str(tmp_path))
    state.write_jsonl("pool.jsonl", [{"note": "from an earlier turn"}, {"source": "a"}])
    assert state.merge_jsonl("pool.jsonl", [{"source": "a", "n": 2}]) == 2
    assert state.read_jsonl("pool.jsonl") == [
        {"note": "from an earlier turn"},
        {"source": "a", "n": 2},
    ]


def test_state_merge_accepts_a_search_hit_directly(tmp_path) -> None:
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
    assert state.merge_jsonl("pool.jsonl", [hit]) == 1
    assert state.merge_jsonl("pool.jsonl", [hit]) == 1
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


def test_output_submission(tmp_path) -> None:
    path = tmp_path / "output.json"
    OutputResource(str(path)).submit(
        {"answer": 42},
        citations=["https://example.com/source"],
    )
    payload = json.loads(path.read_text())
    assert payload["output"] == {"answer": 42}
    assert payload["citations"] == ["https://example.com/source"]


def test_output_citations_do_not_call_the_broker(tmp_path) -> None:
    path = tmp_path / "output.json"
    transport = FakeTransport()
    OutputResource(str(path)).submit({"answer": 42}, citations=["source_1"])
    assert json.loads(path.read_text())["citations"] == ["source_1"]
    assert transport.calls == []


def test_output_accepts_only_bounded_source_strings(tmp_path) -> None:
    output = OutputResource(str(tmp_path / "output.json"))

    with pytest.raises(ValueError, match="input index 0 must be a string"):
        output.submit({}, citations=[{"source": "source_1"}])
    with pytest.raises(ValueError, match="must not be empty"):
        output.submit({}, citations=["  "])
    with pytest.raises(ValueError, match="at most 4096"):
        output.submit({}, citations=["x" * 4097])
    with pytest.raises(ValueError, match="at most 256"):
        output.submit({}, citations=["source"] * 257)

    output.submit({}, citations=["source_1"])
    assert json.loads((tmp_path / "output.json").read_text())["citations"] == ["source_1"]


def test_output_submission_replaces_the_artifact_atomically(tmp_path) -> None:
    path = tmp_path / "output.json"
    path.write_text('{"previous": true}', encoding="utf-8")
    circular: list[object] = []
    circular.append(circular)

    with pytest.raises(ValueError, match="Circular reference"):
        OutputResource(str(path)).submit(circular, citations=["https://example.com/source"])

    assert path.read_text(encoding="utf-8") == '{"previous": true}'
    assert list(tmp_path.iterdir()) == [path]


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
