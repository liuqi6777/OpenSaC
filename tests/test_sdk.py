from __future__ import annotations

import json

import pytest
from opensac_sdk.output import OutputResource
from opensac_sdk.search import SearchResource
from opensac_sdk.state import StateResource


class FakeTransport:
    def __init__(self) -> None:
        self.calls = []

    def call(self, method, params):
        self.calls.append((method, params))
        if method == "citations.resolve":
            return [{"ref": params["refs"][0], "url": "https://example.com"}]
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


def test_search_resource_returns_typed_hits() -> None:
    transport = FakeTransport()
    hits = SearchResource(transport).web("query", limit=3)
    assert hits[0].ref == "ref_1"
    assert transport.calls == [
        ("search.web", {"query": "query", "limit": 3, "offset": 0, "domains": None})
    ]


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


def test_output_rejects_unscoped_citation(tmp_path) -> None:
    with pytest.raises(ValueError, match="ref"):
        OutputResource(str(tmp_path / "output.json"), FakeTransport()).submit(
            {}, citations=[{"url": "https://invented.example"}]
        )
