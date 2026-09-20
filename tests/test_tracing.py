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


@pytest.fixture
def traced_client(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from opensac import Client, provider

    instances = []

    def discover(*, group, name):
        def factory(config, context):
            implementation = {
                "opensac.search": TraceSearch,
                "opensac.fetch": TraceFetch,
                "opensac.rerank": TraceRerank,
                "opensac.llm": TraceLLM,
            }[group]
            instance = implementation()
            instances.append(instance)
            return instance

        return [SimpleNamespace(load=lambda: factory)]

    monkeypatch.setattr(provider, "entry_points", discover)
    with Client(settings=Settings(trace_dir=tmp_path)) as client:
        yield client, instances


def test_sdk_records_final_objects_and_empty_rerank_once(traced_client, tmp_path):
    client, instances = traced_client
    assert client.rerank("query", []) == []
    assert instances == []
    hits = client.search("  query  ")
    ranked = client.rerank("query", hits, top_n=1)
    custom = [{"body": "document", "metadata": 42}]
    custom_ranked = client.rerank("query", custom, text=lambda item: item["body"])

    events = read_events(tmp_path)
    assert len(events) == 4
    assert all(event["layer"] == "sdk" for event in events)
    assert events[0]["input"]["items"] == events[0]["output"] == []
    assert events[1]["input"]["query"] == "query"
    assert events[2]["output"] == [hit.model_dump(mode="json") for hit in ranked]
    assert ranked[0] is hits[0]
    assert events[3]["output"] == custom_ranked
    assert custom_ranked[0] is custom[0]


def test_sdk_records_validation_and_local_callback_failures(traced_client, tmp_path):
    from opensac.errors import InvalidRequestError

    client, instances = traced_client
    with pytest.raises(InvalidRequestError):
        client.search("")
    with pytest.raises(InvalidRequestError):
        client.rerank("query", [], top_n=0)
    with pytest.raises(TypeError):
        client.rerank("query", [object()])
    with pytest.raises(TypeError):
        client.search("query", unexpected=True)
    assert instances == []
    events = read_events(tmp_path)
    assert len(events) == 4
    assert all(event["status"] == "error" for event in events)
    assert events[0]["input"]["query"] == ""
    assert [event["error_type"] for event in events] == [
        "InvalidRequestError",
        "InvalidRequestError",
        "TypeError",
        "TypeError",
    ]
    assert events[2]["input"]["items"] == [{"$unserializable": "object"}]


@pytest.mark.asyncio
async def test_runtime_records_invalid_arguments_and_normalized_inputs(tmp_path):
    from opensac.errors import InvalidRequestError

    runtime = Runtime(Settings(trace_dir=tmp_path), search_provider=TraceSearch())
    try:
        with pytest.raises(InvalidRequestError):
            await runtime.search("")
        with pytest.raises(InvalidRequestError):
            await runtime.search("query", unexpected=True)
        await runtime.search("  query  ")
    finally:
        await runtime.aclose()
    events = read_events(tmp_path)
    assert len(events) == 3
    assert all(event["layer"] == "runtime" for event in events)
    assert events[0]["error_code"] == events[1]["error_code"] == "invalid_request"
    assert events[2]["input"]["query"] == "query"


def test_concurrent_sdk_calls_keep_independent_traces(traced_client, tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    client, _ = traced_client
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(client.search, [f"query-{i}" for i in range(8)]))
    events = read_events(tmp_path)
    assert len(events) == 8
    assert len({event["call_id"] for event in events}) == 8
    assert {event["input"]["query"] for event in events} == {result[0].title for result in results}
    assert all(event["input"]["query"] == event["output"][0]["title"] for event in events)


def test_sdk_batches_and_models_record_one_final_result(traced_client, tmp_path):
    client, _ = traced_client
    results = [
        client.search.many(["one", "two"]),
        client.content.fetch("https://example.com/ok"),
        client.content.fetch_many(["https://example.com/ok", "https://example.com/failed"]),
        client.llm.complete("prompt"),
        client.llm.complete_many(["one", "two"]),
        client.llm.extract("source", {"type": "string"}),
        client.llm.extract_many(["one", "two"], {"type": "string"}),
    ]
    events = read_events(tmp_path)
    assert len(events) == len(results)
    assert all(event["layer"] == "sdk" for event in events)
    for event, result in zip(events, results, strict=True):
        expected = (
            [item.model_dump(mode="json") for item in result]
            if isinstance(result, list)
            else result.model_dump(mode="json")
        )
        assert event["output"] == expected


def test_opaque_rerank_items_do_not_drop_traces_or_invoke_repr(traced_client, tmp_path):
    class Opaque:
        def __repr__(self):
            raise AssertionError("Tracing must not call user repr")

    client, _ = traced_client
    item = Opaque()
    result = client.rerank("query", [item], text=lambda value: "text")
    assert result[0] is item
    event = read_events(tmp_path)[0]
    assert event["status"] == "ok"
    assert event["output"] == [{"$unserializable": "Opaque"}]
