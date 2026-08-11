from __future__ import annotations

from typing import Any

import httpx
import pytest
from opensac_sdk.models import SearchHit

from opensac.backends import local_http
from opensac.backends import serper as serper_module
from opensac.backends.local_http import LocalSearchBackend, parse_document_frontmatter
from opensac.backends.serper import SerperBackend

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


class FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


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
    monkeypatch.setattr(local_http.httpx, "AsyncClient", FakeClient)
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
    client.search_hits = [
        {"docid": 74492, "snippet": DOCUMENT_TEXT, "score": 0.8, "rank": 1}
    ]

    hits = await LocalSearchBackend("http://localhost:8081").search("rumble", limit=5)

    assert hits[0].title == ""
    assert hits[0].date is None
    assert hits[0].snippet == DOCUMENT_TEXT


async def test_offset_deepens_the_request_and_keeps_ranks_absolute(client) -> None:
    client.search_hits = [
        {"docid": index, "snippet": "body", "rank": index} for index in range(1, 21)
    ]
    hits = await LocalSearchBackend("http://localhost:8081").search(
        "q", limit=5, offset=10
    )

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


async def test_local_backend_reuses_and_closes_one_http_client(client) -> None:
    client.search_hits = [{"docid": "1", "snippet": "body", "rank": 1}]
    client.document_text = "body"
    backend = LocalSearchBackend("http://localhost:8081")

    hits = await backend.search("q", limit=1)
    await backend.content(hits)

    assert len(client.instances) == 1
    await backend.aclose()
    assert client.instances[0].closed is True


async def test_content_keeps_the_header_in_the_text(client) -> None:
    """`content.read` addresses lines, so nothing may silently delete one."""
    client.document_text = DOCUMENT_TEXT
    hit = SearchHit(ref="ref_x", backend="local", docid="1", title="", rank=1)
    rows = await LocalSearchBackend("http://localhost:8081").content([hit])

    assert rows[0].text == DOCUMENT_TEXT
    # A hit whose own title was empty still renders one, recovered from the body.
    assert rows[0].title == "Royal Rumble (2020) - Wikipedia"
    assert rows[0].metadata["date"] == "2018-11-19"


async def test_one_unreadable_document_does_not_fail_the_batch(monkeypatch) -> None:
    """The program asked for a batch and can act on a partial one.

    It cannot act on an exception, and it cannot act on a silently shortened
    list either -- so a failure travels back as a row that says so.
    """

    class Failing(FakeClient):
        async def post(self, url, *, json):
            if json.get("docid") == "2":
                raise httpx.ConnectError("boom")
            return FakeResponse({"text": "body"})

    monkeypatch.setattr(local_http.httpx, "AsyncClient", Failing)
    hits = [
        SearchHit(ref=f"ref_{n}", backend="local", docid=str(n), rank=n) for n in (1, 2, 3)
    ]
    rows = await LocalSearchBackend("http://localhost:8081").content(hits)

    assert [row.ref for row in rows] == ["ref_1", "ref_2", "ref_3"]
    assert rows[1].text == ""
    assert "ConnectError" in rows[1].metadata["fetch_error"]
    assert rows[2].text == "body"


def test_backends_declare_what_they_can_and_cannot_do() -> None:
    """The two facts the broker refuses a request on, read off the backend.

    They are declarations rather than checks because the refusal is central:
    one `search` capability can only stay backend-neutral in its name if every
    backend reports its limits in the same vocabulary. The enforcement lives in
    `tests/test_broker.py`.
    """
    assert SerperBackend("key").max_depth == 100
    assert SerperBackend("key").supports_domains is True
    # A dense index over a fixed corpus: bounded by the corpus, not by a
    # service policy, and with no notion of a site to filter on.
    assert LocalSearchBackend("http://localhost").max_depth is None
    assert LocalSearchBackend("http://localhost").supports_domains is False


async def test_web_content_reports_a_page_it_could_not_scrape(monkeypatch) -> None:
    """Paywalls and robots blocks are ordinary on the open web.

    Dropping them makes "nobody could read this" look like "this said
    nothing", and only one of those is worth re-querying over.
    """

    class Blocked:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def post(self, url, *, headers, json):
            raise httpx.HTTPError("403 Forbidden")

    monkeypatch.setattr(serper_module.httpx, "AsyncClient", Blocked)
    hits = [
        SearchHit(ref="ref_1", backend="web", url="https://example.com/a", rank=1),
        SearchHit(ref="ref_2", backend="web", url=None, rank=2),
    ]
    rows = await SerperBackend("key").content(hits)

    assert [row.ref for row in rows] == ["ref_1", "ref_2"]
    assert "HTTPError" in rows[0].metadata["fetch_error"]
    assert "no URL" in rows[1].metadata["fetch_error"]


async def test_serper_reuses_and_closes_one_http_client(monkeypatch) -> None:
    class RecordingClient:
        instances = []

        def __init__(self, *args, **kwargs) -> None:
            self.closed = False
            self.instances.append(self)

        async def post(self, url, *, headers, json):
            if url == SerperBackend.search_url:
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
            return FakeResponse({"text": "page"})

        async def aclose(self) -> None:
            self.closed = True

    monkeypatch.setattr(serper_module.httpx, "AsyncClient", RecordingClient)
    backend = SerperBackend("key")

    hits = await backend.search("query", limit=1)
    assert hits[0].retrieval is not None
    assert hits[0].retrieval.mode == "organic"
    await backend.content(hits)

    assert len(RecordingClient.instances) == 1
    await backend.aclose()
    assert RecordingClient.instances[0].closed is True
