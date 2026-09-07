import math
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from opensac import Client, OpenSACError, provider
from opensac.config import Settings
from opensac.contracts import Completion, SearchHit
from opensac.runtime import Runtime
from opensac.structured import parse_extraction, schema_validator


@contextmanager
def client_for(**settings):
    with Client(settings=Settings(**settings)) as client:
        yield client


@pytest.mark.parametrize("failure", ["missing", "duplicate", "factory", "wrong-method"])
def test_provider_initialization_failures_are_structured_and_batch_aligned(failure, monkeypatch):
    calls = []

    def factory(config, context):
        calls.append(True)
        if failure == "factory":
            raise ValueError("private credential detail")
        return object()

    def discover(**kwargs):
        entry = SimpleNamespace(load=lambda: factory)
        return [] if failure == "missing" else [entry, entry] if failure == "duplicate" else [entry]

    monkeypatch.setattr(provider, "entry_points", discover)
    with client_for() as client:
        with pytest.raises(OpenSACError) as caught:
            client.search("query")
        assert caught.value.code == "configuration_error"
        assert caught.value.status_code == 503
        assert "private" not in str(caught.value)
        batch = client.search.many(["one", "two"])
        assert len(batch) == 2
        assert all(item.error.code == "configuration_error" for item in batch)
        assert len(calls) <= 1  # Failed configuration is not retried per item.


def test_closed_client_has_one_error_for_all_paths():
    with client_for() as client:
        client.close()
        client.close()
        for operation in [
            lambda: client.search("query"),
            lambda: client.search.many([]),
            lambda: client.capabilities(),
            lambda: client.rerank("q", []),
        ]:
            with pytest.raises(OpenSACError) as caught:
                operation()
            assert caught.value.code == "client_closed"


class SearchProvider:
    def __init__(self, config=None, context=None):
        self.closed = 0

    async def search(self, query, limit):
        return [
            SearchHit(url=f"http://localhost/{i}", title=query, snippet="text") for i in range(4)
        ]

    async def aclose(self):
        self.closed += 1


def test_core_enforces_search_limit_for_third_party_providers(monkeypatch):
    monkeypatch.setattr(
        provider, "entry_points", lambda **kwargs: [SimpleNamespace(load=lambda: SearchProvider)]
    )
    with client_for() as client:
        assert len(client.search("query", limit=1)) == 1
        batch = client.search.many(["one", "two"], limit=2)
        assert [len(item.data) for item in batch] == [2, 2]


@pytest.mark.parametrize("name", ["$id", "$ref", "$dynamicRef", "$schema"])
def test_schema_keyword_names_can_be_data_properties(name):
    schema = {
        "type": "object",
        "properties": {name: {"type": "string"}},
        "required": [name],
        "additionalProperties": False,
    }
    import json

    assert parse_extraction(json.dumps({name: "value"}), schema_validator(schema)) == {
        name: "value"
    }


@pytest.mark.parametrize(
    "schema",
    [
        {"properties": {"value": {"$ref": "https://example.com/schema"}}},
        {"$defs": {"value": {"$id": "https://example.com"}}},
        {"items": {"$ref": "https://example.com/schema"}},
        {"allOf": [{"$ref": "https://example.com/schema"}]},
    ],
)
def test_nested_external_schema_resources_still_rejected(schema):
    with pytest.raises(OpenSACError) as caught:
        schema_validator(schema)
    assert caught.value.code == "invalid_schema"


class ModelProvider:
    output = '{"value":1e400}'

    def __init__(self, config=None, context=None):
        pass

    async def complete(self, prompt, *, schema=None):
        return Completion(text=self.output, model="test")

    async def aclose(self):
        pass


def test_overflow_is_a_failure_instead_of_a_successful_null(monkeypatch):
    monkeypatch.setattr(
        provider, "entry_points", lambda **kwargs: [SimpleNamespace(load=lambda: ModelProvider)]
    )
    schema = {"type": "object", "properties": {"value": {"type": "number"}}}
    with client_for() as client:
        with pytest.raises(OpenSACError) as caught:
            client.llm.extract("text", schema)
        assert caught.value.code == "structured_output_invalid"
        batch = client.llm.extract_many(["one", "two"], schema)
        assert all(item.error.code == "structured_output_invalid" for item in batch)
    assert math.isfinite(parse_extraction("1e300", schema_validator({"type": "number"})))


@pytest.mark.asyncio
async def test_provider_ownership_closes_shared_instances_once_even_after_failure():
    class Shared(SearchProvider):
        async def fetch(self, url):
            raise NotImplementedError

        async def aclose(self):
            self.closed += 1
            raise ValueError("cleanup failure")

    shared = Shared()
    another = SearchProvider()
    runtime = Runtime(search_provider=shared, fetch_provider=shared, rerank_provider=another)
    with pytest.raises(ExceptionGroup):
        await runtime.aclose()
    await runtime.aclose()
    assert shared.closed == 1
    assert another.closed == 1
