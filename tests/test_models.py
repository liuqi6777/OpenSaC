import asyncio
import json
from contextlib import contextmanager
from types import SimpleNamespace

import httpx
import pytest
from pydantic import HttpUrl, SecretStr

from opensac import Client, OpenSACError, provider
from opensac.config import Settings
from opensac.contracts import Completion, RerankResult
from opensac.errors import CapabilityError
from opensac.provider import ProviderConfig, ProviderContext, load_provider
from opensac.providers.http import HTTPRerank
from opensac.providers.openai import OpenAILLM
from opensac.runtime import Runtime

SCHEMA = {
    "type": "object",
    "properties": {"count": {"type": "integer"}},
    "required": ["count"],
    "additionalProperties": False,
}


class FakeLLM:
    def __init__(self, config, context):
        self.requests = []
        self.closed = False
        self.active = self.peak = 0

    async def complete(self, prompt, *, schema=None):
        self.requests.append((prompt, schema))
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(0.005)
            if "timeout" in prompt:
                raise CapabilityError("provider_timeout", "Timed out.", 504, True)
            text = '{"count": "wrong"}' if "invalid" in prompt else '{"count": 3}'
            return Completion(text=text if schema else "answer", model="test-model")
        finally:
            self.active -= 1

    async def aclose(self):
        self.closed = True


class FakeRerank:
    def __init__(self, config, context):
        self.closed = False

    async def rerank(self, query, documents, *, top_n=None):
        return [
            RerankResult(index=i, relevance_score=i / 10) for i in reversed(range(len(documents)))
        ][:top_n]

    async def aclose(self):
        self.closed = True


@pytest.fixture
def factories(monkeypatch):
    instances = []

    def discover(*, group, name):
        factory = {"opensac.rerank": FakeRerank, "opensac.llm": FakeLLM}[group]

        def create(config, context):
            obj = factory(config, context)
            instances.append(obj)
            return obj

        return [SimpleNamespace(load=lambda: create)]

    monkeypatch.setattr(provider, "entry_points", discover)
    return instances


@contextmanager
def client_for():
    with Client(settings=Settings(max_concurrency=2)) as client:
        yield client


def test_models_share_contract_batches_and_lazy_lifecycle(factories):
    with client_for() as sdk:
        assert factories == []
        assert "llm.extract" in sdk.capabilities().methods
        ranked = sdk.rerank("query", ["first", "second"], top_n=1)
        assert ranked == ["second"]
        assert len(factories) == 1
        assert sdk.llm.complete("hello").text == "answer"
        extracted = sdk.llm.extract("three items", SCHEMA)
        assert extracted.data == {"count": 3}
        assert json.loads(extracted.model_dump_json())["data"] == {"count": 3}
        completed = sdk.llm.complete_many(["ok", "timeout", "again", "four"])
        assert completed[0].data.text == "answer"
        assert completed[1].error.code == "provider_timeout"
        assert completed[2].data.text == "answer"
        assert factories[1].peak == 2
        results = sdk.llm.extract_many(["valid", "invalid", "timeout"], SCHEMA)
        assert results[0].data.data == {"count": 3}
        assert results[1].error.code == "structured_output_invalid"
        assert results[2].error.retryable
    assert all(instance.closed for instance in factories)


def test_invalid_inputs_do_not_initialize_providers(factories):
    with client_for() as sdk:
        for operation in [
            lambda: sdk.rerank("query", ["text"], top_n=2),
            lambda: sdk.llm.complete(""),
            lambda: sdk.llm.complete_many([]),
            lambda: sdk.llm.extract("text", {"type": "not-a-type"}),
            lambda: sdk.llm.extract("text", {"$ref": "#/$defs/missing"}),
            lambda: sdk.llm.extract("text", {"$ref": "https://example.com/schema"}),
            lambda: sdk.llm.extract_many(["text"], {"$id": "https://example.com"}),
        ]:
            with pytest.raises(OpenSACError) as caught:
                operation()
            assert caught.value.code in {"invalid_request", "invalid_schema"}
        assert factories == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "results",
    [
        [{"index": 2, "relevance_score": 1}],
        [{"index": 0, "relevance_score": 1}, {"index": 0, "relevance_score": 0}],
        [{"index": 0, "relevance_score": float("nan")}],
        [],
    ],
)
async def test_invalid_ranking_rejected(results):
    class BadRanker(FakeRerank):
        async def rerank(self, query, documents, *, top_n=None):
            return results

    runtime = Runtime(rerank_provider=BadRanker(None, None))
    try:
        with pytest.raises(CapabilityError) as caught:
            await runtime.rerank("query", ["a", "b"])
        assert caught.value.code == "provider_invalid_response"
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_builtin_providers_request_shapes_and_usage():

    requests = []

    def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        assert request.headers["Authorization"] == "Bearer fake-key"
        if request.url.path == "/v1/rerank":
            assert body == {
                "model": "rank-model",
                "query": "q",
                "documents": ["a", "b"],
                "top_n": 1,
            }
            return httpx.Response(200, json={"results": [{"index": 1, "relevance_score": 0.9}]})
        assert request.url.path == "/v1/chat/completions"
        assert body["messages"] == [{"role": "user", "content": "prompt"}]
        assert body["model"] == "configured"
        assert body["max_completion_tokens"] == 20
        assert "temperature" not in body
        assert body["response_format"]["json_schema"]["schema"] == SCHEMA
        return httpx.Response(
            200,
            json={
                "model": "actual-model",
                "choices": [{"finish_reason": "stop", "message": {"content": '{"count": 3}'}}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 4, "total_tokens": 11},
            },
        )

    context = ProviderContext()
    ranker = HTTPRerank(
        ProviderConfig(
            base_url=HttpUrl("http://local/v1"), model="rank-model", api_key=SecretStr("fake-key")
        ),
        context,
        transport=httpx.MockTransport(handle),
    )
    llm = OpenAILLM(
        ProviderConfig(
            base_url=HttpUrl("http://local/v1"),
            model="configured",
            max_tokens=20,
            api_key=SecretStr("fake-key"),
        ),
        context,
        transport=httpx.MockTransport(handle),
    )
    try:
        assert (await ranker.rerank("q", ["a", "b"], top_n=1))[0].index == 1
        completion = await llm.complete("prompt", schema=SCHEMA)
        assert completion.usage.total_tokens == 11
        assert completion.model == "actual-model"
    finally:
        await ranker.aclose()
        await llm.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload,code",
    [
        ({"choices": [{"message": {"refusal": "sensitive"}}]}, "model_refusal"),
        ({"choices": [{"message": {}, "finish_reason": "length"}]}, "generation_incomplete"),
        ({"choices": []}, "provider_invalid_response"),
        (
            {"choices": [{"message": {"content": None}, "finish_reason": "stop"}]},
            "provider_invalid_response",
        ),
    ],
)
async def test_llm_failures_are_explicit_and_sanitized(payload, code):

    llm = OpenAILLM(
        ProviderConfig(base_url=HttpUrl("http://local/v1"), model="model"),
        ProviderContext(),
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)),
    )
    try:
        with pytest.raises(CapabilityError) as caught:
            await llm.complete("prompt")
        assert caught.value.code == code
        assert "sensitive" not in str(caught.value)
    finally:
        await llm.aclose()


@pytest.mark.asyncio
async def test_model_entry_points_are_independent_and_missing_config_is_lazy():

    ranker = load_provider("rerank", "http", ProviderConfig(), ProviderContext())
    llm = load_provider("llm", "openai", ProviderConfig(), ProviderContext())
    try:
        assert not hasattr(ranker, "complete")
        assert not hasattr(llm, "rerank")
        for operation in [
            ranker.rerank("q", ["a"]),
            llm.complete("q"),
        ]:
            with pytest.raises(CapabilityError) as caught:
                await operation
            assert caught.value.code == "not_configured"
    finally:
        await ranker.aclose()
        await llm.aclose()


@pytest.mark.parametrize("text", ['{"count": NaN}', '```json\n{"count": 3}\n```', '{"count": "3"}'])
def test_extraction_requires_actual_json_and_schema_types(text):
    from opensac.structured import parse_extraction, schema_validator

    with pytest.raises(CapabilityError) as caught:
        parse_extraction(text, schema_validator(SCHEMA))
    assert caught.value.code == "structured_output_invalid"


def test_schema_references_are_unsupported():
    from opensac.structured import parse_extraction, schema_validator

    schema = {"$defs": {"count": {"type": "integer"}}, "$ref": "#/$defs/count"}
    with pytest.raises(CapabilityError) as unsupported:
        schema_validator(schema)
    assert unsupported.value.code == "invalid_schema"
    with pytest.raises(CapabilityError) as caught:
        parse_extraction("3", schema_validator({"$ref": "#/$defs/missing"}))
    assert caught.value.code == "invalid_schema"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_class,capability", [(HTTPRerank, "rerank"), (OpenAILLM, "llm")])
@pytest.mark.parametrize(
    "failure,code",
    [
        ("rate", "provider_rate_limited"),
        ("size", "response_too_large"),
        ("timeout", "provider_timeout"),
    ],
)
async def test_model_adapters_preserve_transport_limits(provider_class, capability, failure, code):

    def handle(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("sensitive upstream detail")
        if failure == "rate":
            return httpx.Response(429, text="sensitive upstream detail")
        return httpx.Response(200, content=b"x" * 2048)

    provider = provider_class(
        ProviderConfig(base_url=HttpUrl("http://local/v1"), model="model"),
        ProviderContext(max_response_bytes=1024),
        transport=httpx.MockTransport(handle),
    )
    try:
        with pytest.raises(CapabilityError) as caught:
            if capability == "llm":
                await provider.complete("prompt")
            else:
                await provider.rerank("q", ["a"])
        assert caught.value.code == code
        assert "sensitive" not in str(caught.value)
    finally:
        await provider.aclose()


def test_rerank_preserves_original_objects_and_supports_custom_text(factories):

    from opensac import Document, SearchHit

    with client_for() as sdk:
        assert sdk.rerank("query", []) == []
        assert factories == []
        hits = [
            SearchHit(url=f"http://localhost/{i}", title=f"title {i}", snippet="snippet")
            for i in range(3)
        ]
        best = sdk.rerank("query", hits, top_n=2)
        assert best[0] is hits[2]
        assert best[1] is hits[1]
        documents = [Document(url=hit.url, text="body") for hit in hits]
        assert sdk.rerank("query", documents, top_n=1)[0] is documents[2]
        custom = [{"body": "first", "metadata": object()}, {"body": "second"}]
        assert sdk.rerank("query", custom, text=lambda item: item["body"])[0] is custom[1]
        with pytest.raises(TypeError):
            sdk.rerank("query", custom)
        with pytest.raises(OpenSACError):
            sdk.rerank("query", hits, top_n=4)


def test_simple_extraction_without_subprocesses(factories, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("SDK extraction must not spawn a process")

    monkeypatch.setattr("subprocess.Popen", forbidden)
    with client_for() as client:
        assert client.llm.extract("count", SCHEMA).data == {"count": 3}
        calls = len(factories[0].requests)
        with pytest.raises(OpenSACError) as exc:
            client.llm.extract("count", {"type": "string", "pattern": ".*"})
        assert exc.value.code == "invalid_schema"
        assert len(factories[0].requests) == calls


def test_rerank_sends_only_selected_text(monkeypatch):
    from opensac import SearchHit

    class InspectRerank(FakeRerank):
        async def rerank(self, query, documents, *, top_n=None):
            assert documents == ["Title\nSnippet"]
            return [RerankResult(index=0, relevance_score=0.8)]

    monkeypatch.setattr(
        provider, "entry_points", lambda **kwargs: [SimpleNamespace(load=lambda: InspectRerank)]
    )
    with Client() as sdk:
        hit = SearchHit(url="http://localhost/private", title="Title", snippet="Snippet")
        assert sdk.rerank("query", [hit])[0] is hit


@pytest.mark.parametrize(
    "documents,top_n",
    [(["a"], 2), (["a"], True), (["a"], 0), (["a" * 200_000] * 3, None)],
)
@pytest.mark.asyncio
async def test_rerank_input_limits_precede_provider_loading(documents, top_n, factories):
    runtime = Runtime()
    try:
        with pytest.raises(CapabilityError) as caught:
            await runtime.rerank("query", documents, top_n=top_n)
        assert caught.value.code == "invalid_request"
        assert factories == []
    finally:
        await runtime.aclose()


def test_host_generation_configuration_reaches_provider_requests(monkeypatch):
    def handle(request):
        body = json.loads(request.content)
        assert body["model"] == "host-model"
        assert body["max_completion_tokens"] == 77
        assert body["temperature"] == 0.25
        return httpx.Response(
            200,
            json={
                "model": "host-model",
                "choices": [{"finish_reason": "stop", "message": {"content": '{"count": 3}'}}],
            },
        )

    monkeypatch.setenv("OPENSAC_LLM_MODEL", "host-model")
    monkeypatch.setenv("OPENSAC_LLM_MAX_TOKENS", "77")
    monkeypatch.setenv("OPENSAC_LLM_TEMPERATURE", "0.25")
    monkeypatch.setattr(
        provider,
        "entry_points",
        lambda **kwargs: [
            SimpleNamespace(
                load=lambda: (
                    lambda config, context: OpenAILLM(
                        config, context, transport=httpx.MockTransport(handle)
                    )
                )
            )
        ],
    )
    with Client(settings=Settings(llm_base_url="http://local/v1")) as client:
        assert client.llm.complete("hello").model == "host-model"
        assert client.llm.extract("three", SCHEMA).data == {"count": 3}
        assert client.llm.complete_many(["one", "two"])[1].ok
        assert client.llm.extract_many(["one"], SCHEMA)[0].data.data == {"count": 3}


def test_sdk_does_not_accept_generation_overrides(factories):
    with Client() as client:
        for call in [
            lambda: client.llm.complete("hello", model="override"),
            lambda: client.llm.complete_many(["hello"], max_tokens=1),
            lambda: client.llm.extract("text", SCHEMA, temperature=0.5),
            lambda: client.llm.extract_many(["text"], SCHEMA, model="override"),
            lambda: client.rerank("query", ["text"], model="override"),
        ]:
            with pytest.raises(TypeError):
                call()
        assert factories == []
