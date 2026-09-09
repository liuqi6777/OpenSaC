import json
from pathlib import Path

import pytest

from opensac.config import Settings
from opensac.contracts import Completion, Document, RerankResult, SearchHit
from opensac.errors import CapabilityError
from opensac.runtime import Runtime
from opensac.tracing import Tracer


class TraceSearch:
    async def search(self, query: str, limit: int) -> list[SearchHit]:
        return [SearchHit(url="https://private.example/result", title=query, snippet="Evidence")]

    async def aclose(self) -> None:
        pass


class TraceFetch:
    async def fetch(self, url: str) -> Document:
        if url.endswith("failed"):
            raise CapabilityError("provider_timeout", "Reader timed out.", 504, True)
        return Document(url=url, text="private fetched body")

    async def aclose(self) -> None:
        pass


class TraceRerank:
    async def rerank(
        self, query: str, documents: list[str], *, top_n: int | None = None
    ) -> list[RerankResult]:
        count = top_n or len(documents)
        return [
            RerankResult(index=index, relevance_score=float(count - index))
            for index in range(count)
        ]

    async def aclose(self) -> None:
        pass


class TraceLLM:
    async def complete(self, prompt: str, *, schema: dict[str, object] | None = None) -> Completion:
        return Completion(text='"value"' if schema is not None else "answer", model="trace-model")

    async def aclose(self) -> None:
        pass


def read_events(trace_dir: Path) -> list[dict[str, object]]:
    paths = list(trace_dir.glob("opensac-*.jsonl"))
    assert len(paths) == 1
    return [json.loads(line) for line in paths[0].read_text(encoding="utf-8").splitlines()]


@pytest.mark.asyncio
async def test_trace_records_flat_operation_inputs_and_returned_values(tmp_path: Path):
    runtime = Runtime(
        Settings(
            trace_dir=tmp_path,
            trace_run_id="research-1",
            trace_action_id="codex-turn-03",
        ),
        search_provider=TraceSearch(),
        fetch_provider=TraceFetch(),
        rerank_provider=TraceRerank(),
        llm_provider=TraceLLM(),
    )
    try:
        hits = await runtime.search(["first query", "second query"])
        await runtime.fetch(hits[0].url)
        await runtime.fetch_many(["https://private.example/ok", "https://private.example/failed"])
        await runtime.rerank("ranking query", ["first", "second"])
        await runtime.complete("private completion prompt")
        await runtime.extract("private source text", {"type": "string"})
    finally:
        await runtime.aclose()

    events = read_events(tmp_path)
    assert {event["event"] for event in events} == {"operation.call"}
    assert {event["operation"] for event in events} == {
        "search",
        "fetch",
        "fetch_many",
        "rerank",
        "llm.complete",
        "llm.extract",
    }
    assert all(event["run_id"] == "research-1" for event in events)
    assert all(event["action_id"] == "codex-turn-03" for event in events)
    assert all(event["duration_ms"] >= 0 for event in events)
    assert all("parent_span_id" not in event and "provider" not in event for event in events)

    search_event = next(event for event in events if event["operation"] == "search")
    assert search_event["input"]["query"] == ["first query", "second query"]
    assert search_event["output"] == [hit.model_dump(mode="json") for hit in hits]

    batch_event = next(event for event in events if event["operation"] == "fetch_many")
    assert batch_event["input"]["urls"] == [
        "https://private.example/ok",
        "https://private.example/failed",
    ]
    assert batch_event["output"][0]["data"]["text"] == "private fetched body"
    assert batch_event["output"][1]["error"]["code"] == "provider_timeout"

    serialized = json.dumps(events)
    assert "private completion prompt" in serialized
    assert "private source text" in serialized
    assert "private fetched body" in serialized


@pytest.mark.asyncio
async def test_trace_writer_failure_does_not_change_research_result(tmp_path: Path):
    trace_path = tmp_path / "not-a-directory"
    trace_path.write_text("block tracing", encoding="utf-8")
    runtime = Runtime(Settings(trace_dir=trace_path), search_provider=TraceSearch())
    try:
        assert (await runtime.search("still works"))[0].title == "still works"
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_trace_is_disabled_without_a_directory(tmp_path: Path):
    runtime = Runtime(Settings(), fetch_provider=TraceFetch())
    try:
        document = await runtime.fetch("https://private.example/ordinary")
        assert document.text == "private fetched body"
    finally:
        await runtime.aclose()
    assert list(tmp_path.glob("opensac-*.jsonl")) == []


@pytest.mark.asyncio
async def test_generated_action_id_is_stable_for_one_process(tmp_path: Path):
    runtime = Runtime(Settings(trace_dir=tmp_path), search_provider=TraceSearch())
    try:
        await runtime.search("first")
        await runtime.search("second")
    finally:
        await runtime.aclose()

    action_ids = {event["action_id"] for event in read_events(tmp_path)}
    assert len(action_ids) == 1
    assert action_ids != {None}


def test_closed_tracer_does_not_reopen_its_output(tmp_path: Path):
    tracer = Tracer(trace_dir=tmp_path, run_id="research-1", action_id="action-1")
    record = {
        "call_id": "call-1",
        "operation": "search",
        "started_at": "2026-01-01T00:00:00+00:00",
        "duration_ms": 1.0,
        "inputs": {"query": "first"},
        "output": [],
    }
    tracer.record(**record)
    tracer.close()

    trace_path = next(tmp_path.glob("opensac-*.jsonl"))
    line_count = len(trace_path.read_text(encoding="utf-8").splitlines())
    tracer.record(**record)

    assert len(trace_path.read_text(encoding="utf-8").splitlines()) == line_count
