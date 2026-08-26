from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from opensac.backends._response import json_object
from opensac.backends.document import DocumentContent, DocumentHandle
from opensac.backends.document import jina as jina_module
from opensac.backends.document import local_http as local_document_module
from opensac.backends.document.jina import JinaReaderBackend
from opensac.backends.document.local_http import (
    LocalDocumentBackend,
    parse_document_frontmatter,
)
from opensac.backends.llm import LLMResponse, OpenAICompatibleBackend
from opensac.backends.rerank import RerankScore
from opensac.backends.search import local_http as local_search_module
from opensac.backends.search import serper as serper_module
from opensac.backends.search.local_http import LocalSearchBackend
from opensac.backends.search.serper import SerperBackend
from opensac.provider import ProviderRequestError

# What `/get_document` returns: the full document, including its YAML header.
DOCUMENT_TEXT = (
    "---\n"
    "title: Royal Rumble (2020) - Wikipedia\n"
    "date: 2018-11-19\n"
    "author: Contributors\n"
    "---\n"
    "Royal Rumble (2020) - Wikipedia\n"
    "The 2020 Royal Rumble was the 33rd Royal Rumble.\n"
)


def test_backend_output_models_are_strict_and_immutable() -> None:
    with pytest.raises(ValidationError):
        LLMResponse(content="answer", tokens=True)
    with pytest.raises(ValidationError):
        RerankScore(index=True, score=1.0)
    with pytest.raises(ValidationError):
        DocumentContent(source="source", text=1)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        DocumentHandle(source=1)  # type: ignore[arg-type]

    response = LLMResponse(content="answer", tokens=1)
    with pytest.raises(ValidationError, match="frozen"):
        response.tokens = 2
    handle = DocumentHandle(source="source")
    with pytest.raises(ValidationError, match="frozen"):
        handle.source = "other"


class FakeResponse:
    def __init__(self, payload: Any, *, text: str = "") -> None:
        self._payload = payload
        self.text = text

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        return self._payload


def test_provider_json_object_accepts_mapping_payload() -> None:
    assert json_object(FakeResponse({"result": "ok"})) == {"result": "ok"}


def test_provider_json_object_rejects_non_object_payload() -> None:
    with pytest.raises(ProviderRequestError) as raised:
        json_object(FakeResponse(["not", "an", "object"]))

    assert raised.value.code == "provider_invalid_response"


def test_provider_json_object_sanitizes_decode_failure() -> None:
    class InvalidJsonResponse:
        def json(self) -> Any:
            raise ValueError("secret provider response")

    with pytest.raises(ProviderRequestError) as raised:
        json_object(InvalidJsonResponse())

    assert raised.value.code == "provider_invalid_response"
    assert "secret provider response" not in str(raised.value)


class FakeClient:
    """Stands in for httpx.AsyncClient, recording what the backend asked for."""

    requests: list[tuple[str, dict[str, Any]]] = []
    search_hits: list[dict[str, Any]] = []
    batch_results: list[dict[str, Any]] = []
    document_text: str = ""
    retrieval_backend: str = "dense"
    result_mode: str = "query_aware"
    instances: list[FakeClient] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.closed = False
        type(self).instances.append(self)

    async def __aenter__(self) -> FakeClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def aclose(self) -> None:
        self.closed = True

    async def post(self, url: str, *, json: dict[str, Any]) -> FakeResponse:
        type(self).requests.append((url, json))
        if url.endswith("search_many"):
            return FakeResponse(
                {
                    "backend": self.retrieval_backend,
                    "result_mode": self.result_mode,
                    "results": self.batch_results,
                }
            )
        if url.endswith("search"):
            return FakeResponse(
                {
                    "backend": self.retrieval_backend,
                    "result_mode": self.result_mode,
                    "results": [{"hits": self.search_hits}],
                }
            )
        return FakeResponse({"text": self.document_text})


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> type[FakeClient]:
    FakeClient.requests = []
    FakeClient.search_hits = []
    FakeClient.batch_results = []
    FakeClient.document_text = ""
    FakeClient.retrieval_backend = "dense"
    FakeClient.result_mode = "query_aware"
    FakeClient.instances = []
    monkeypatch.setattr(local_search_module.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(local_document_module.httpx, "AsyncClient", FakeClient)
    return FakeClient


def test_frontmatter_is_parsed_leniently() -> None:
    fields, body = parse_document_frontmatter(DOCUMENT_TEXT)
    assert fields["title"] == "Royal Rumble (2020) - Wikipedia"
    assert fields["date"] == "2018-11-19"
    assert body.startswith("Royal Rumble (2020) - Wikipedia\n")
    # A document without a header is returned untouched rather than mangled.
    assert parse_document_frontmatter("plain body") == ({}, "plain body")


async def test_search_uses_server_shaped_fields_without_reprocessing(client) -> None:
    client.search_hits = [
        {
            "docid": 74492,
            "title": "Royal Rumble (2020) - Wikipedia",
            "date": "2018-11-19",
            "snippet": "The server-selected passage about the 33rd Royal Rumble.",
            "score": 0.8,
            "rank": 1,
            "retrieval_debug": "dense",
        }
    ]
    hits = await LocalSearchBackend("http://localhost:8081").search("rumble", limit=5)

    assert hits[0].title == "Royal Rumble (2020) - Wikipedia"
    assert hits[0].date == "2018-11-19"
    assert hits[0].docid == "74492"
    assert hits[0].snippet == "The server-selected passage about the 33rd Royal Rumble."
    assert hits[0].metadata == {"retrieval_debug": "dense"}
    assert hits[0].retrieval is not None
    assert hits[0].retrieval.mode == "dense"
    assert hits[0].retrieval.result_mode == "query_aware"
    assert hits[0].retrieval.higher_is_better is True
    assert hits[0].retrieval.comparable_across_queries is False


async def test_search_does_not_parse_a_full_mode_snippet(client) -> None:
    """Even a frontmatter-looking snippet remains owned by the search server."""
    client.search_hits = [{"docid": 74492, "snippet": DOCUMENT_TEXT, "score": 0.8, "rank": 1}]

    hits = await LocalSearchBackend("http://localhost:8081").search("rumble", limit=5)

    assert hits[0].title == ""
    assert hits[0].date is None
    assert hits[0].snippet == DOCUMENT_TEXT


async def test_offset_deepens_the_request_and_keeps_ranks_absolute(client) -> None:
    client.search_hits = [
        {"docid": index, "snippet": "body", "rank": index} for index in range(1, 21)
    ]
    hits = await LocalSearchBackend("http://localhost:8081").search("q", limit=5, offset=10)

    # The service has no offset parameter, so depth is asked for and sliced.
    assert client.requests[0][1] == {"query": "q", "top_k": 15}
    assert [hit.docid for hit in hits] == ["11", "12", "13", "14", "15"]
    # Rank is the position in the full ranking, not in the returned window --
    # anything joining a trace against qrels depends on that.
    assert [hit.rank for hit in hits] == [11, 12, 13, 14, 15]


async def test_local_search_many_uses_one_request_and_preserves_order(client) -> None:
    client.batch_results = [
        {
            "query": "beta",
            "hits": [
                {
                    "docid": "2",
                    "title": "Beta title",
                    "date": "2026-08-11",
                    "snippet": "beta",
                    "rank": 1,
                }
            ],
        },
        {"query": "alpha", "hits": [{"docid": "1", "snippet": "alpha", "rank": 1}]},
        {"query": "beta", "hits": [{"docid": "3", "snippet": "beta 2", "rank": 1}]},
    ]
    backend = LocalSearchBackend("http://localhost:8081")

    batches = await backend.search_many(["beta", "alpha", "beta"], limit=4)

    assert [batch.query for batch in batches] == ["beta", "alpha", "beta"]
    assert [batch.hits[0].docid for batch in batches] == ["2", "1", "3"]
    assert batches[0].hits[0].title == "Beta title"
    assert batches[0].hits[0].date == "2026-08-11"
    assert batches[0].hits[0].snippet == "beta"
    assert batches[0].hits[0].retrieval is not None
    assert batches[0].hits[0].retrieval.mode == "dense"
    assert len(client.requests) == 1
    assert client.requests[0][0].endswith("/search_many")
    assert client.requests[0][1] == {
        "queries": ["beta", "alpha", "beta"],
        "top_k": 4,
    }


async def test_local_backends_use_independent_reusable_http_clients(client) -> None:
    client.search_hits = [{"docid": "1", "snippet": "body", "rank": 1}]
    client.document_text = "body"
    search_backend = LocalSearchBackend("http://localhost:8081")
    document_backend = LocalDocumentBackend("http://localhost:8081")

    hits = await search_backend.search("q", limit=1)
    await document_backend.fetch(DocumentHandle(source="1", docid=hits[0].docid))

    assert len(client.instances) == 2
    await search_backend.aclose()
    await document_backend.aclose()
    assert all(instance.closed for instance in client.instances)


async def test_content_keeps_the_header_in_the_text(client) -> None:
    """`content.read` addresses lines, so nothing may silently delete one."""
    client.document_text = DOCUMENT_TEXT
    hit = DocumentHandle(source="doc_x", docid="1")
    row = await LocalDocumentBackend("http://localhost:8081").fetch(hit)

    assert row.text == DOCUMENT_TEXT
    # A hit whose own title was empty still renders one, recovered from the body.
    assert row.title == "Royal Rumble (2020) - Wikipedia"
    assert row.metadata["date"] == "2018-11-19"


async def test_local_fetch_is_one_atomic_transport_operation(monkeypatch) -> None:
    """Partial-failure shaping belongs above the single-document adapter."""

    class Failing(FakeClient):
        async def post(self, url, *, json):
            if json.get("docid") == "2":
                raise httpx.ConnectError("boom")
            return FakeResponse({"text": "body"})

    monkeypatch.setattr(local_document_module.httpx, "AsyncClient", Failing)
    hits = [DocumentHandle(source=f"doc_{n}", docid=str(n)) for n in (1, 2, 3)]
    backend = LocalDocumentBackend("http://localhost:8081")

    assert (await backend.fetch(hits[0])).text == "body"
    with pytest.raises(httpx.ConnectError, match="boom"):
        await backend.fetch(hits[1])
    assert (await backend.fetch(hits[2])).text == "body"


async def test_local_adapter_rejects_invalid_success_payload(monkeypatch) -> None:
    class Invalid(FakeClient):
        async def post(self, url, *, json):
            if url.endswith("search"):
                return FakeResponse({"results": []})
            return FakeResponse({"unexpected": "shape"})

    monkeypatch.setattr(local_search_module.httpx, "AsyncClient", Invalid)
    monkeypatch.setattr(local_document_module.httpx, "AsyncClient", Invalid)
    search_backend = LocalSearchBackend("http://localhost:8081")
    document_backend = LocalDocumentBackend("http://localhost:8081")

    with pytest.raises(ProviderRequestError) as search_error:
        await search_backend.search("q", limit=1)
    assert search_error.value.code == "provider_invalid_response"

    hit = DocumentHandle(source="doc_1", docid="1")
    with pytest.raises(ProviderRequestError) as fetch_error:
        await document_backend.fetch(hit)
    assert fetch_error.value.code == "provider_invalid_response"


def test_backends_declare_what_they_can_and_cannot_do() -> None:
    """The two facts the broker refuses a request on, read off the backend.

    They are declarations rather than checks because the refusal is central:
    one `search` capability can only stay backend-neutral in its name if every
    backend reports its limits in the same vocabulary. The enforcement lives in
    `tests/test_broker.py`.
    """
    assert SerperBackend("key").max_depth == 100
    assert SerperBackend("key").supports_domains is True
    assert SerperBackend("key").result_cacheable is True
    assert not hasattr(SerperBackend("key"), "fetch")
    # A dense index over a fixed corpus: bounded by the corpus, not by a
    # service policy, and with no notion of a site to filter on.
    assert LocalSearchBackend("http://localhost").max_depth is None
    assert LocalSearchBackend("http://localhost").supports_domains is False
    assert LocalSearchBackend("http://localhost").result_cacheable is False
    assert not hasattr(LocalSearchBackend("http://localhost"), "fetch")

    local_document = LocalDocumentBackend("http://localhost")
    local_hit = DocumentHandle(source="doc", docid="doc")
    assert local_document.source_kind == "opaque"
    assert local_document.result_cacheable is False
    assert local_document.fetch_candidates(local_hit) == [local_hit]

    reader = JinaReaderBackend("key")
    archive_hit = DocumentHandle(
        source="https://archive.org/details/book",
        url="https://archive.org/details/book",
    )
    assert reader.source_kind == "public_url"
    assert reader.result_cacheable is True
    assert [candidate.url for candidate in reader.fetch_candidates(archive_hit)] == [
        "https://archive.org/details/book",
        "https://archive.org/download/book/book_djvu.txt",
    ]


async def test_web_fetch_exposes_transport_and_typed_input_failures(monkeypatch) -> None:
    """The provider runtime, not the adapter, shapes partial-failure rows."""

    class Blocked:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def get(self, url, *, headers):
            raise httpx.HTTPError("403 Forbidden")

    monkeypatch.setattr(jina_module.httpx, "AsyncClient", Blocked)
    hits = [
        DocumentHandle(source="doc_1", url="https://example.com/a"),
        DocumentHandle(source="doc_2", url=None),
    ]
    backend = JinaReaderBackend("key")

    with pytest.raises(httpx.HTTPError, match="403"):
        await backend.fetch(hits[0])
    with pytest.raises(ProviderRequestError) as caught:
        await backend.fetch(hits[1])
    assert caught.value.code == "invalid_request"


def test_bundled_fetch_preflight_validates_handles_and_credentials() -> None:
    local_hit = DocumentHandle(source="doc_local", docid=None)
    with pytest.raises(ProviderRequestError) as local_error:
        LocalDocumentBackend("http://localhost:8081").preflight_fetch(local_hit)
    assert local_error.value.code == "invalid_request"

    for invalid_url in (None, "example.com/page", "javascript:alert(1)"):
        web_hit = DocumentHandle(
            source="doc_web",
            url=invalid_url,
        )
        with pytest.raises(ProviderRequestError) as web_error:
            JinaReaderBackend("key").preflight_fetch(web_hit)
        assert web_error.value.code == "invalid_request"

    configured_hit = DocumentHandle(
        source="doc_web",
        url="https://example.com/page",
    )
    JinaReaderBackend("").preflight_fetch(configured_hit)


async def test_web_backends_use_independent_reusable_http_clients(monkeypatch) -> None:
    class RecordingClient:
        instances = []

        def __init__(self, *args, **kwargs) -> None:
            self.closed = False
            self.instances.append(self)

        async def post(self, url, *, headers, json):
            return FakeResponse(
                {
                    "organic": [
                        {
                            "title": "A",
                            "link": "https://example.com/a",
                            "snippet": "summary",
                        }
                    ]
                }
            )

        async def get(self, url, *, headers):
            assert url == "https://r.jina.ai/https://example.com/a"
            assert headers == {"Authorization": "Bearer jina-secret"}
            return FakeResponse({}, text="page")

        async def aclose(self) -> None:
            self.closed = True

    monkeypatch.setattr(serper_module.httpx, "AsyncClient", RecordingClient)
    monkeypatch.setattr(jina_module.httpx, "AsyncClient", RecordingClient)
    search_backend = SerperBackend("key")
    document_backend = JinaReaderBackend("jina-secret")

    hits = await search_backend.search("query", limit=1)
    assert hits[0].retrieval is not None
    assert hits[0].retrieval.mode == "organic"
    await document_backend.fetch(
        DocumentHandle(source=hits[0].url or "", url=hits[0].url, title=hits[0].title)
    )

    assert len(RecordingClient.instances) == 2
    await search_backend.aclose()
    await document_backend.aclose()
    assert all(instance.closed for instance in RecordingClient.instances)


async def test_jina_reader_rejects_blank_success_text(monkeypatch) -> None:
    class BlankReader:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def get(self, url, *, headers):
            return FakeResponse({}, text=" \n\t")

    monkeypatch.setattr(jina_module.httpx, "AsyncClient", BlankReader)
    backend = JinaReaderBackend("key")
    hit = DocumentHandle(
        source="https://example.com/blank",
        url="https://example.com/blank",
    )

    with pytest.raises(ProviderRequestError, match="empty document text") as caught:
        await backend.fetch(hit)

    assert caught.value.code == "provider_invalid_response"
    assert caught.value.scope == "resource"
    assert backend.provider_name == "jina_reader"


async def test_serper_missing_credentials_is_a_zero_attempt_preflight_failure() -> None:
    backend = SerperBackend("")

    with pytest.raises(ProviderRequestError) as preflight:
        backend.preflight_search()
    assert preflight.value.code == "provider_not_configured"
    assert preflight.value.attempts == 0

    with pytest.raises(ProviderRequestError) as caught:
        await backend.search("query", limit=1)
    assert caught.value.code == "provider_not_configured"
    assert caught.value.attempts == 0


def test_provider_identity_is_stable_and_does_not_expose_credentials() -> None:
    first = SerperBackend("top-secret")
    second = SerperBackend("top-secret")
    other = SerperBackend("different-secret")
    reader = JinaReaderBackend("jina-secret")
    same_reader = JinaReaderBackend("jina-secret")
    other_reader = JinaReaderBackend("different-jina-secret")

    assert first.provider_identity == second.provider_identity
    assert first.provider_identity != other.provider_identity
    assert reader.provider_identity == same_reader.provider_identity
    assert reader.provider_identity != other_reader.provider_identity
    assert "top-secret" not in first.provider_identity
    assert "jina-secret" not in reader.provider_identity
    assert LocalSearchBackend("http://localhost:8081").provider_identity.startswith("local-search:")
    assert LocalDocumentBackend("http://localhost:8081").provider_identity.startswith(
        "local-document:"
    )


async def test_openai_compatible_backend_normalizes_requests_usage_and_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RecordingClient:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []
            self.closed = False
            parent = self

            class Completions:
                async def create(self, **kwargs):
                    parent.calls.append(kwargs)
                    return SimpleNamespace(
                        choices=[SimpleNamespace(message=SimpleNamespace(content="answer"))],
                        usage=SimpleNamespace(total_tokens=17),
                    )

            self.chat = SimpleNamespace(completions=Completions())

        async def close(self) -> None:
            self.closed = True

    client = RecordingClient()
    clients_created = 0

    def new_client(_backend: OpenAICompatibleBackend) -> RecordingClient:
        nonlocal clients_created
        clients_created += 1
        return client

    monkeypatch.setattr(OpenAICompatibleBackend, "_new_client", new_client)
    backend = OpenAICompatibleBackend(
        model="test-model",
        api_key="model-secret",
        base_url="https://models.example/v1",
    )
    assert backend._client is None

    response = await backend.complete(
        "prompt",
        system="system",
        temperature=0.4,
        max_tokens=50,
        json_object=True,
    )

    assert response.content == "answer"
    assert response.tokens == 17
    assert clients_created == 1
    assert client.calls == [
        {
            "model": "test-model",
            "messages": [
                {"role": "system", "content": "system"},
                {"role": "user", "content": "prompt"},
            ],
            "temperature": 0.4,
            "max_completion_tokens": 50,
            "response_format": {"type": "json_object"},
        }
    ]
    assert backend.provider_name == "openai_compatible"
    assert "model-secret" not in backend.provider_identity
    await backend.aclose()
    assert client.closed is True
