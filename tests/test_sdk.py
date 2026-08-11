from __future__ import annotations

import json
from unittest.mock import patch

import httpx
import pytest
from opensac_sdk.llm import LLMResource
from opensac_sdk.models import (
    ContentMatch,
    ContentSnippet,
    EvidenceLocator,
    ExtractionError,
    ExtractionResult,
    RetrievalMetadata,
    SearchBatch,
    SearchHit,
    SearchRequestInfo,
)
from opensac_sdk.output import OutputResource
from opensac_sdk.search import SearchResource
from opensac_sdk.state import StateResource
from opensac_sdk.transport import BrokerError, UnixSocketTransport
from pydantic import ValidationError


class FakeTransport:
    def __init__(self) -> None:
        self.calls = []

    def call(self, method, params):
        self.calls.append((method, params))
        if method == "citations.resolve":
            requested = params.get("requests")
            ref = requested[0]["ref"] if requested else params["refs"][0]
            return [{"ref": ref, "url": "https://example.com"}]
        return [
            {
                "ref": "ref_1",
                "backend": "web",
                "title": "Title",
                "url": "https://example.com",
                "domain": "example.com",
                "snippet": "text",
                "rank": 1,
            }
        ]


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


def test_search_resource_returns_typed_hits() -> None:
    transport = FakeTransport()
    hits = SearchResource(transport)("query", limit=3)
    assert hits[0].ref == "ref_1"
    assert transport.calls == [
        ("search.query", {"query": "query", "limit": 3, "offset": 0, "domains": None})
    ]


def test_search_many_attaches_the_effective_request_to_each_batch() -> None:
    class ManyTransport:
        def call(self, method, params):
            assert method == "search.query_many"
            return [
                {"query": query, "hits": [], "error": None}
                for query in params["queries"]
            ]

    batches = SearchResource(ManyTransport()).many(
        ["one", "two"],
        limit_per_query=12,
        offset=4,
        domains=["example.com"],
    )

    assert [batch.request.model_dump() for batch in batches if batch.request] == [
        {"limit": 12, "offset": 4, "domains": ["example.com"]},
        {"limit": 12, "offset": 4, "domains": ["example.com"]},
    ]


def _hit(ref: str, rank: int, *, backend: str = "local", score: float | None = None):
    return SearchHit(
        ref=ref,
        backend=backend,
        title=ref,
        rank=rank,
        score=score,
        retrieval=RetrievalMetadata(
            mode="dense",
            result_mode="query_aware",
            score_name="backend_score",
            higher_is_better=True,
            comparable_across_queries=False,
        ),
    )


def test_search_rrf_fuses_refs_locally_and_preserves_provenance() -> None:
    transport = FakeTransport()
    search = SearchResource(transport)
    batches = [
        SearchBatch(
            query="alpha",
            hits=[_hit("a", 1, score=0.9), _hit("a", 3), _hit("b", 2)],
            request=SearchRequestInfo(limit=3, offset=0, domains=["example.com"]),
        ),
        SearchBatch(
            query="beta",
            hits=[_hit("b", 1), _hit("a", 2)],
            request=SearchRequestInfo(limit=2, offset=10),
        ),
        SearchBatch(query="failed", hits=[_hit("ignored", 1)], error="timeout"),
    ]

    result = search.fuse_rrf(batches, weights=[1, 2, 1])

    assert transport.calls == []
    assert [candidate.ref for candidate in result.candidates] == ["b", "a"]
    assert [candidate.fused_rank for candidate in result.candidates] == [1, 2]
    assert result.input_count == 5
    assert result.unique_count == 2
    assert result.duplicate_count == 3
    assert result.batch_errors[0].model_dump() == {
        "batch_index": 2,
        "query": "failed",
        "error": "timeout",
    }

    candidate_a = result.candidates[1]
    assert candidate_a.rank == 1
    assert len(candidate_a.sources) == 2
    assert candidate_a.sources[0].request is not None
    assert candidate_a.sources[0].request.domains == ["example.com"]
    assert candidate_a.sources[0].retrieval is not None
    assert candidate_a.sources[0].retrieval.mode == "dense"


def test_search_rrf_has_stable_ties_limit_and_empty_input() -> None:
    search = SearchResource(FakeTransport())
    tied = search.fuse_rrf(
        [
            SearchBatch(query="first", hits=[_hit("z", 1)]),
            SearchBatch(query="second", hits=[_hit("a", 1)]),
        ],
        limit=1,
    )
    assert [candidate.ref for candidate in tied.candidates] == ["z"]
    assert tied.unique_count == 2

    empty = search.fuse_rrf([])
    assert empty.candidates == []
    assert empty.model_dump(exclude={"candidates", "batch_errors"}) == {
        "input_count": 0,
        "unique_count": 0,
        "duplicate_count": 0,
    }


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
        SearchBatch(query="one", hits=[_hit("a", 1)]),
        SearchBatch(query="two", hits=[_hit("b", 1)]),
    ]
    with pytest.raises(ValueError, match=message):
        SearchResource(FakeTransport()).fuse_rrf(batches, **kwargs)


def test_search_rrf_refuses_non_positive_source_rank() -> None:
    with pytest.raises(ValueError, match="rank"):
        SearchResource(FakeTransport()).fuse_rrf(
            [SearchBatch(query="bad", hits=[_hit("a", 0)])]
        )


class ExtractionTransport:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def call(self, method, params):
        self.calls.append((method, params))
        return self.result


def test_extract_many_returns_typed_per_item_results_and_forwards_repair() -> None:
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

    assert results[0] == ExtractionResult(index=0, data={"matches": True}, attempts=1)
    assert results[1].error == ExtractionError(
        code="schema_mismatch",
        message="matches is required",
        retryable=False,
    )
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


def test_extraction_result_requires_exactly_one_data_or_error() -> None:
    with pytest.raises(ValidationError, match="exactly one"):
        ExtractionResult(index=0, attempts=1)
    with pytest.raises(ValidationError, match="exactly one"):
        ExtractionResult(
            index=0,
            data={},
            error=ExtractionError(code="x", message="x", retryable=False),
            attempts=1,
        )


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
    assert state.merge_jsonl("pool.jsonl", [{"ref": "a", "n": 1}, {"ref": "b", "n": 1}]) == 2
    # A document a second query returned replaces its row in place rather than
    # adding a second one, and does not move ahead of documents found later.
    assert state.merge_jsonl("pool.jsonl", [{"ref": "c", "n": 1}, {"ref": "a", "n": 2}]) == 3
    assert state.read_jsonl("pool.jsonl") == [
        {"ref": "a", "n": 2},
        {"ref": "b", "n": 1},
        {"ref": "c", "n": 1},
    ]
    # Any field can be the identity; ``ref`` is only the common one.
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
    state.write_jsonl("pool.jsonl", [{"note": "from an earlier turn"}, {"ref": "a"}])
    assert state.merge_jsonl("pool.jsonl", [{"ref": "a", "n": 2}]) == 2
    assert state.read_jsonl("pool.jsonl") == [
        {"note": "from an earlier turn"},
        {"ref": "a", "n": 2},
    ]


def test_state_merge_accepts_a_search_hit_directly(tmp_path) -> None:
    """Same contract as ``write_jsonl``: no ``.model_dump()`` at the call site."""
    state = StateResource(str(tmp_path))
    hit = SearchHit(
        ref="ref_1", backend="local", title="t", url=None, docid="7", domain=None,
        date=None, snippet="s", score=1.0, rank=1,
    )
    assert state.merge_jsonl("pool.jsonl", [hit]) == 1
    assert state.merge_jsonl("pool.jsonl", [hit]) == 1
    assert state.read_jsonl("pool.jsonl")[0].docid == "7"


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
    transport = FakeTransport()
    OutputResource(str(path), transport).submit({"answer": 42}, citations=[{"ref": "ref_1"}])
    payload = json.loads(path.read_text())
    assert payload["output"] == {"answer": 42}
    assert payload["citations"][0]["url"] == "https://example.com"


def test_output_forwards_an_evidence_locator_without_flattening_it(tmp_path) -> None:
    path = tmp_path / "output.json"
    transport = FakeTransport()
    locator = EvidenceLocator(id="ev_1", ref="ref_1", kind="selected_passage")

    OutputResource(str(path), transport).submit(
        {"answer": 42},
        citations=[{"ref": "ref_1", "locator": locator}],
    )

    assert transport.calls == [
        (
            "citations.resolve",
            {
                "requests": [
                    {
                        "ref": "ref_1",
                        "locator": {
                            "id": "ev_1",
                            "ref": "ref_1",
                            "kind": "selected_passage",
                        },
                    }
                ]
            },
        )
    ]


def test_content_models_accept_optional_evidence_locators() -> None:
    locator = {"id": "ev_1", "ref": "ref_1", "kind": "selected_passage"}
    snippet = ContentSnippet(ref="ref_1", text="passage", locator=locator)
    match = ContentMatch(ref="ref_1", line=8, text="match", locator=locator)

    assert snippet.locator is not None and snippet.locator.id == "ev_1"
    assert match.locator is not None and match.locator.kind == "selected_passage"


def test_output_rejects_unscoped_citation(tmp_path) -> None:
    with pytest.raises(ValueError, match="ref"):
        OutputResource(str(tmp_path / "output.json"), FakeTransport()).submit(
            {}, citations=[{"url": "https://invented.example"}]
        )


def test_a_result_answers_to_either_spelling_of_a_field_read() -> None:
    """One type, two access styles -- not two representations tolerating each other.

    Programs reach for `hit["docid"]` because that is what every search API
    they have read returns, and the attribute-only form turns that prior into
    `'SearchHit' object is not subscriptable`, which ends the turn.
    """
    hit = SearchResource(FakeTransport())("query")[0]
    assert hit["ref"] == hit.ref == "ref_1"
    assert hit.get("title") == "Title"
    assert hit.get("nonexistent") is None
    assert hit.get("nonexistent", "fallback") == "fallback"
    assert "ref" in hit and "nonexistent" not in hit
    assert dict(hit)["rank"] == 1
    # Neither spelling can reach past the fields, so they cannot disagree.
    with pytest.raises(KeyError):
        hit["nonexistent"]


def test_a_snippet_carries_the_date_of_the_hit_it_came_from() -> None:
    """`SearchHit.date` exists because time-constrained tasks are common.

    A snippet with `title` and `url` but no `date` reads like an oversight, and
    a program written on that assumption dies rather than skipping a filter.
    """
    assert "date" in ContentSnippet.model_fields
    snippet = ContentSnippet(ref="ref_1", text="body", date="1994")
    assert snippet.date == snippet["date"] == "1994"


def test_a_result_written_to_the_workspace_comes_back_readable(tmp_path) -> None:
    """The round trip is where the type is lost, so it must not lose the access.

    JSON cannot carry a Python type, so a hit written in one turn returns as a
    mapping in the next. Programs go on writing `row.ref` because that is how
    every other line around it is written.
    """
    state = StateResource(str(tmp_path))
    hit = SearchResource(FakeTransport())("query")[0]

    # Passed straight in: `default=str` would have written the repr instead,
    # and a later turn subscripting that string would get a character.
    state.write_jsonl("pool.jsonl", [hit])
    assert json.loads((tmp_path / "pool.jsonl").read_text())["ref"] == "ref_1"

    row = state.read_jsonl("pool.jsonl")[0]
    assert row.ref == row["ref"] == "ref_1"
    assert row.rank == 1
    # Still an ordinary dict: it survives json, unpacking and the dict methods.
    assert isinstance(row, dict)
    assert json.dumps(row)
    assert {**row}["title"] == "Title"
    assert sorted(row.keys())[:2] == ["backend", "date"]


def test_a_row_says_what_it_has_when_a_field_is_missing(tmp_path) -> None:
    state = StateResource(str(tmp_path))
    state.write_jsonl("pool.jsonl", [{"ref": "ref_1", "rank": 2}])
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
