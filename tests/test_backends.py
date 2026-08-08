from __future__ import annotations

from typing import Any

import pytest
from opensac_sdk.models import SearchHit

from opensac.backends import local_http
from opensac.backends.local_http import LocalSearchBackend, parse_document_frontmatter

# What the retrieval service actually returns: the document's YAML header is
# inside `snippet`, and the body then repeats the title as its first line.
SNIPPET = (
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
    document_text: str = ""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> FakeClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def post(self, url: str, *, json: dict[str, Any]) -> FakeResponse:
        type(self).requests.append((url, json))
        if url.endswith("search"):
            return FakeResponse({"results": [{"hits": self.search_hits}]})
        return FakeResponse({"text": self.document_text})


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> type[FakeClient]:
    FakeClient.requests = []
    FakeClient.search_hits = []
    FakeClient.document_text = ""
    monkeypatch.setattr(local_http.httpx, "AsyncClient", FakeClient)
    return FakeClient


def test_frontmatter_is_parsed_leniently() -> None:
    fields, body = parse_document_frontmatter(SNIPPET)
    assert fields["title"] == "Royal Rumble (2020) - Wikipedia"
    assert fields["date"] == "2018-11-19"
    assert body.startswith("Royal Rumble (2020) - Wikipedia\n")
    # A document without a header is returned untouched rather than mangled.
    assert parse_document_frontmatter("plain body") == ({}, "plain body")


async def test_search_lifts_title_and_date_out_of_the_snippet(client) -> None:
    """The cheapest triage a program can do must not return a column of blanks.

    Printing candidate titles is the first thing a program writes. With
    `SearchHit.title` falling through to its empty default, that print came
    back as `- [74492]` for every row, and the only ways forward were dumping
    raw snippets (which fills the output budget) or searching again.
    """
    client.search_hits = [{"docid": 74492, "snippet": SNIPPET, "score": 0.8, "rank": 1}]
    hits = await LocalSearchBackend("http://localhost:8081").search("rumble", limit=5)

    assert hits[0].title == "Royal Rumble (2020) - Wikipedia"
    assert hits[0].date == "2018-11-19"
    assert hits[0].docid == "74492"
    # Everything else the header declared stays reachable without a schema change.
    assert hits[0].metadata["author"] == "Contributors"
    # The header and the duplicated title line are gone from the snippet: both
    # are now carried as fields, and snippet space is the scarce resource.
    assert hits[0].snippet == "The 2020 Royal Rumble was the 33rd Royal Rumble."


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


async def test_content_keeps_the_header_in_the_text(client) -> None:
    """`content.read` addresses lines, so nothing may silently delete one."""
    client.document_text = SNIPPET
    hit = SearchHit(ref="ref_x", backend="local", docid="1", title="", rank=1)
    rows = await LocalSearchBackend("http://localhost:8081").content([hit])

    assert rows[0].text == SNIPPET
    # A hit whose own title was empty still renders one, recovered from the body.
    assert rows[0].title == "Royal Rumble (2020) - Wikipedia"
    assert rows[0].metadata["date"] == "2018-11-19"
