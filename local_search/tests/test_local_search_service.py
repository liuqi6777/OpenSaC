from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from fastapi.testclient import TestClient
from local_search.api import create_app
from local_search.prepare import REQUIRED_FILES, download_index
from local_search.searcher import (
    LocalSearchHit,
    _load_corpus,
    _load_index_ids,
    _validate_metadata,
)

from local_search import prepare, snippets


class FakeEncoding:
    def encode(self, text: str) -> list[int]:
        return list(text.encode())

    def decode(self, token_ids: list[int]) -> str:
        return bytes(token_ids).decode(errors="ignore")


class FakeDenseSearcher:
    backend_name = "dense"
    index_metadata = {"schema": "test", "version": 1}

    def search(self, query: str, k: int) -> list[LocalSearchHit]:
        return [
            LocalSearchHit(
                docid="doc-1",
                score=0.75,
                snippet="---\ntitle: Example\ndate: 2026-01-02\n---\nExample\nBody text",
            )
        ][:k]

    def search_many(self, queries: list[str], k: int) -> list[list[LocalSearchHit]]:
        return [self.search(query, k) for query in queries]

    def get_document(self, docid: str):
        if docid != "doc-1":
            return None
        return {"docid": docid, "text": "Body text"}


def test_local_search_api_contract(monkeypatch) -> None:
    monkeypatch.setattr(snippets, "_get_snippet_encoding", FakeEncoding)
    app = create_app(searcher=FakeDenseSearcher(), result_mode="compact")

    with TestClient(app) as client:
        health = client.get("/healthz")
        search = client.post("/search", json={"query": "body", "top_k": 1})
        batch = client.post(
            "/search_many", json={"queries": ["one", "two"], "top_k": 1}
        )
        document = client.post("/get_document", json={"docid": "doc-1"})

    assert health.json()["backend"] == "dense"
    assert search.json()["results"][0]["hits"][0] == {
        "docid": "doc-1",
        "score": 0.75,
        "snippet": "Body text",
        "rank": 1,
        "title": "Example",
        "date": "2026-01-02",
    }
    assert [row["query"] for row in batch.json()["results"]] == ["one", "two"]
    assert document.json() == {"docid": "doc-1", "text": "Body text"}


def test_index_ids_and_corpus_are_joined_by_docid(tmp_path: Path) -> None:
    ids_path = tmp_path / "index_ids.json"
    corpus_path = tmp_path / "corpus.jsonl"
    ids_path.write_text(json.dumps(["second", "first"]), encoding="utf-8")
    corpus_path.write_text(
        '{"id":"first","contents":"one"}\n'
        '{"docid":"second","text":"two"}\n',
        encoding="utf-8",
    )

    assert _load_index_ids(ids_path) == ["second", "first"]
    documents, digest = _load_corpus(corpus_path)
    assert documents == {"first": "one", "second": "two"}
    assert len(digest) == 64


def test_prepare_downloads_only_required_files(monkeypatch, tmp_path: Path) -> None:
    calls = []

    def fake_download(**kwargs):
        calls.append(kwargs)
        kwargs["destination"].touch()

    monkeypatch.setattr(prepare, "_download_file", fake_download)

    paths = download_index(
        repo_id="owner/repo",
        output_dir=tmp_path,
        revision="abc123",
    )

    assert [path.name for path in paths] == list(REQUIRED_FILES)
    assert [call["filename"] for call in calls] == list(REQUIRED_FILES)
    assert all(call["revision"] == "abc123" for call in calls)


def test_metadata_rejects_query_model_mismatch() -> None:
    metadata = {
        "model_name": "other/model",
        "max_length": 32768,
        "pooling": "last_token_left_padding_v1",
        "vector_count": 2,
        "ordered_corpus_sha256": "a" * 64,
    }

    try:
        _validate_metadata(
            metadata,
            model_name="Qwen/Qwen3-Embedding-8B",
            max_length=32768,
            vector_count=2,
            corpus_sha256="a" * 64,
        )
    except ValueError as exc:
        assert "model_name" in str(exc)
    else:
        raise AssertionError("model mismatch should be rejected")


def test_run_script_is_executable_and_documents_short_commands() -> None:
    script = Path(__file__).resolve().parents[1] / "run"

    assert os.access(script, os.X_OK)
    subprocess.run(["sh", "-n", str(script)], check=True)
    result = subprocess.run(
        [str(script), "--help"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "./local_search/run setup" in result.stdout
    assert "./local_search/run prepare" in result.stdout
    assert "./local_search/run [ARGS]" in result.stdout


def test_run_script_does_not_auto_install_missing_environment(tmp_path: Path) -> None:
    script = Path(__file__).resolve().parents[1] / "run"
    environment = os.environ | {"LOCAL_SEARCH_VENV": str(tmp_path / "missing")}

    result = subprocess.run(
        [str(script)],
        capture_output=True,
        text=True,
        env=environment,
    )

    assert result.returncode == 1
    assert "run ./local_search/run setup" in result.stderr
    assert not (tmp_path / "missing").exists()
