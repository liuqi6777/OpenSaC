"""Execute skill examples against signature-checked SDK doubles, without external I/O."""

from __future__ import annotations

import json
import runpy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import create_autospec

import opensac_sdk
import pytest
from opensac_sdk._record import wrap
from opensac_sdk._resources import ContentResource, SearchResource

from opensac.sandbox.validator import validate_code

EXAMPLES = Path(__file__).parents[1] / ".agents/skills/search-as-code-cli-v2/examples"


@pytest.fixture
def sdk_double(monkeypatch, tmp_path):
    client = SimpleNamespace(
        search=create_autospec(SearchResource, instance=True),
        content=create_autospec(ContentResource, instance=True),
    )
    monkeypatch.setattr(opensac_sdk, "sdk", client)
    monkeypatch.chdir(tmp_path)
    return client


def run_example(name):
    path = EXAMPLES / name
    validate_code(path.read_text())
    return runpy.run_path(str(path))


def hit(source):
    return wrap({"source": source, "title": "Python", "snippet": "Candidate text"})


@pytest.mark.parametrize("hits, status", [(None, "failed"), ([], "ok")])
def test_search_distinguishes_failure_from_empty_success(sdk_double, capsys, hits, status):
    sdk_double.search.return_value = hits
    result = run_example("web_search.py")
    observation = result["observation"]
    assert observation["status"] == status
    if hits is not None:
        assert observation["total"] == 0


def test_batch_resume_retries_only_failed_input_and_retains_provenance(sdk_double, capsys):
    sdk_double.search.many.return_value = [[hit("doc-1")], None]
    first = run_example("fanout_research.py")
    queries = first["QUERIES"]
    saved = json.loads(first["STATE_PATH"].read_text())
    assert saved["failed_queries"] == [queries[1]]
    assert saved["candidates"][0]["queries"] == [queries[0]]

    sdk_double.search.many.reset_mock()
    sdk_double.search.many.return_value = [[hit("doc-1"), hit("doc-2")]]
    run_example("fanout_research.py")
    sdk_double.search.many.assert_called_once_with([queries[1]], limit=5)
    saved = json.loads(first["STATE_PATH"].read_text())
    assert len(saved["candidates"]) == 2
    assert saved["candidates"][0]["queries"] == queries
    assert saved["failed_queries"] == []

    sdk_double.search.many.reset_mock()
    run_example("fanout_research.py")
    sdk_double.search.many.assert_not_called()
    capsys.readouterr()


def test_empty_batch_is_saved_and_not_researched(sdk_double):
    sdk_double.search.many.return_value = [[], []]
    run_example("fanout_research.py")
    sdk_double.search.many.reset_mock()
    run_example("fanout_research.py")
    sdk_double.search.many.assert_not_called()


@pytest.mark.parametrize("prefix", ["x" * 1200, "x" * 1200 + "\n" + "y" * 1200 + "\n"])
def test_local_inspection_locates_long_line_match(sdk_double, capsys, prefix):
    text = prefix + "free-threading" + "z" * 1000
    sdk_double.search.return_value = [hit("doc-1"), hit("doc-1")]
    sdk_double.content.fetch_many.return_value = [wrap({"source": "doc-1", "text": text})]
    result = run_example("snippets_verify.py")
    assert result["match"].span() == (len(prefix), len(prefix) + len("free-thread"))
    assert result["start"] <= result["match"].start()
    assert result["end"] >= result["match"].end()
    assert result["documents"][0]["text"] == text
    assert len(capsys.readouterr().out) < 4000
    sdk_double.content.fetch_many.assert_called_once_with(["doc-1"])


def test_partial_fetch_does_not_prevent_inspecting_success(sdk_double):
    sdk_double.search.return_value = [hit("doc-1"), hit("doc-2")]
    document = {"source": "doc-2", "text": "free-threading context"}
    sdk_double.content.fetch_many.return_value = [None, document]
    result = run_example("snippets_verify.py")
    assert result["source"] == "doc-2"
    assert result["match"].span() == (0, len("free-thread"))
    sdk_double.content.fetch_many.assert_called_once_with(["doc-1", "doc-2"])


@pytest.mark.parametrize("hits", [None, []])
def test_unavailable_candidates_do_not_trigger_fetch(sdk_double, hits):
    sdk_double.search.return_value = hits
    run_example("snippets_verify.py")
    sdk_double.content.fetch_many.assert_not_called()
