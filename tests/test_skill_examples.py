"""Exercise research examples with local providers and temporary task artifacts."""

import json
import runpy
import sys
from pathlib import Path

import pytest
from conftest import FakeProvider

import opensac
from opensac import Client
from opensac.contracts import Document, SearchHit
from opensac.errors import ProviderTimeoutError

EXAMPLES = Path(__file__).resolve().parents[1] / "skills" / "search-as-code" / "examples"


@pytest.mark.parametrize("unmatched_body", ["", "Unrelated document text"])
def test_evidence_keeps_passages_documents_and_failed_sources(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    sdk_client: Client,
    provider: FakeProvider,
    unmatched_body: str,
) -> None:
    urls = [f"https://example.com/{name}" for name in ("match", "unmatched", "failed")]
    body = (
        "Navigation\n" * 200
        + "Feature is experimental, not enabled by default.\n"
        + "Details\n" * 80
    )
    fetched: list[str] = []

    async def search(query: str, limit: int) -> list[SearchHit]:
        return [SearchHit(url=url, title=query, snippet="Preview") for url in [urls[0], *urls]]

    async def fetch(url: str) -> Document:
        fetched.append(url)
        if url == urls[2]:
            raise ProviderTimeoutError("Reader timed out.")
        return Document(url=url, text=body if url == urls[0] else unmatched_body)

    monkeypatch.setattr(provider, "search", search)
    monkeypatch.setattr(provider, "fetch", fetch)
    monkeypatch.setattr(opensac, "sdk", sdk_client)
    output = tmp_path / "evidence"
    monkeypatch.setattr(
        sys,
        "argv",
        ["evidence.py", "feature status", "--pattern", "experimental", "--output", str(output)],
    )
    with pytest.raises(SystemExit) as exited:
        runpy.run_path(str(EXAMPLES / "evidence.py"), run_name="__main__")
    assert exited.value.code == 1
    assert sorted(fetched) == sorted(urls)
    report = json.loads((output / "evidence.json").read_text())
    assert report["query"] == "feature status"
    matched, unmatched, failed = report["sources"]
    assert matched["input_url"] == urls[0]
    assert matched["url"] == urls[0]
    assert matched["status"] == "needs_review"
    document = Document.model_validate_json((output / matched["document"]).read_text())
    assert document.text == body
    for passage in matched["passages"]:
        assert passage["start"] > 1500
        assert passage["text"] == body[passage["start"] : passage["end"]]
        assert passage["start"] <= body.index("experimental") < passage["end"]
    assert unmatched["status"] == ("no_match" if unmatched_body else "empty_text")
    assert unmatched["passages"] == []
    assert failed["input_url"] == urls[2]
    assert failed["status"] == "fetch_failed"
    assert failed["error"]["code"] == "provider_timeout"
    assert provider.closed


def test_evidence_empty_search_skips_fetch_and_saves_the_gap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, sdk_client: Client, provider: FakeProvider
) -> None:
    monkeypatch.setattr(opensac, "sdk", sdk_client)
    output = tmp_path / "empty-evidence"
    monkeypatch.setattr(
        sys, "argv", ["evidence.py", "empty", "--pattern", "term", "--output", str(output)]
    )
    with pytest.raises(SystemExit) as exited:
        runpy.run_path(str(EXAMPLES / "evidence.py"), run_name="__main__")
    assert exited.value.code == 0
    assert provider.calls == ["empty"]
    assert json.loads((output / "search.json").read_text())["hits"] == []
    assert json.loads((output / "evidence.json").read_text())["sources"] == []


def test_fanout_preserves_each_question_and_deduplicates_fetches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, sdk_client: Client, provider: FakeProvider
) -> None:
    async def search(query: str, limit: int) -> list[SearchHit]:
        topic = "threading" if "free threading" in query else "jit"
        urls = ["https://example.com/shared", f"https://example.com/{topic}"]
        return [SearchHit(url=url, title=query, snippet="Preview") for url in urls]

    monkeypatch.setattr(provider, "search", search)
    monkeypatch.setattr(opensac, "sdk", sdk_client)
    monkeypatch.chdir(tmp_path)
    result = runpy.run_path(str(EXAMPLES / "fanout_research.py"))
    assert result["fetch_urls"] == [
        "https://example.com/shared",
        "https://example.com/threading",
        "https://example.com/jit",
    ]
    assert result["provenance"]["https://example.com/shared"] == result["QUERIES"]
    state = json.loads(result["STATE_PATH"].read_text())
    assert set(state["batches"]) == set(result["QUERIES"])


def test_fanout_resume_does_not_replay_empty_searches_or_failures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, sdk_client: Client, provider: FakeProvider
) -> None:
    calls: list[str] = []

    async def search(query: str, limit: int) -> list[SearchHit]:
        calls.append(query)
        if "JIT" in query:
            raise ProviderTimeoutError("Search timed out.")
        return []

    monkeypatch.setattr(provider, "search", search)
    monkeypatch.setattr(opensac, "sdk", sdk_client)
    monkeypatch.chdir(tmp_path)
    first = runpy.run_path(str(EXAMPLES / "fanout_research.py"))
    second = runpy.run_path(str(EXAMPLES / "fanout_research.py"))
    assert sorted(calls) == sorted(first["QUERIES"])
    assert second["fetch_urls"] == []
    state = json.loads(first["STATE_PATH"].read_text())
    assert state["batches"][first["QUERIES"][0]] == []
    assert state["errors"][first["QUERIES"][1]]["code"] == "provider_timeout"
