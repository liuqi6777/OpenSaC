from __future__ import annotations

import asyncio
import re
from urllib.parse import urljoin

import httpx
from opensac_sdk.models import ContentSnippet, SearchHit

# Documents in the local corpus carry a YAML frontmatter header ahead of the
# body, and the body then repeats the title as its own first line:
#
#     ---
#     title: Royal Rumble (2020) - Wikipedia
#     date: 2018-11-19
#     ---
#     Royal Rumble (2020) - Wikipedia
#     The 2020 Royal Rumble was ...
#
# The retrieval service returns that header inside `snippet`, so the fields
# were always present -- they were simply never parsed, and `SearchHit.title`
# fell through to its empty default. Printing a candidate list, which is the
# cheapest triage a program can do, came back as a column of blanks.
#
# Deliberately not a YAML parser: the header is a flat `key: value` block, and
# taking a dependency to read it would let a malformed document raise where an
# unparsed line should just be skipped.
_FRONTMATTER_PATTERN = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*\n?", re.DOTALL)


def parse_document_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Split ``text`` into its frontmatter fields and the body below them.

    Mirrors ``DeepResearch-dev/src/tools/tool_search.py::_parse_document_frontmatter``
    so a document renders with the same title and date under Search as Code as
    it does under the function-calling profiles it is compared against. Returns
    ``({}, text)`` unchanged when there is no header.
    """
    match = _FRONTMATTER_PATTERN.match(text)
    if match is None:
        return {}, text
    fields: dict[str, str] = {}
    for line in match.group(1).splitlines():
        key, separator, value = line.partition(":")
        key = key.strip().lower()
        # First occurrence wins, and a line without a colon is skipped rather
        # than treated as a key with an empty value.
        if separator and key and key not in fields:
            fields[key] = value.strip()
    return fields, text[match.end() :]


def _strip_repeated_title(body: str, title: str) -> str:
    """Drop the body's first line when it merely repeats the title.

    The snippet budget is the scarce resource here; spending its first line on
    a string already carried by ``SearchHit.title`` is pure loss.
    """
    body = body.strip()
    if not title:
        return body
    first_line, separator, remainder = body.partition("\n")
    if first_line.strip() == title:
        return remainder.strip() if separator else ""
    return body


class LocalSearchBackend:
    name = "local"

    def __init__(
        self,
        base_url: str,
        timeout: float = 30.0,
        fetch_concurrency: int = 6,
    ) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        self.timeout = timeout
        # The retriever behind this is the same process every other tool
        # profile queries, so an unbounded fan-out here does not only slow this
        # run down, it perturbs the thing the comparison holds fixed.
        self._fetch_gate = asyncio.Semaphore(max(1, fetch_concurrency))

    async def search(
        self,
        query: str,
        *,
        limit: int,
        offset: int = 0,
        domains: list[str] | None = None,
    ) -> list[SearchHit]:
        del domains
        # The retrieval service has no offset parameter, so depth is reached by
        # asking for the whole prefix and discarding it. Wasteful on the wire
        # and cheap in practice (it is a local dense index), and it keeps the
        # backend honest about ranks: a hit's `rank` stays its rank in the full
        # result list rather than its position in the returned window, which is
        # what fusion and any offline qrels join need.
        depth = offset + limit
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                urljoin(self.base_url, "search"),
                json={"query": query, "top_k": depth},
            )
            response.raise_for_status()
        payload = response.json()
        rows = payload.get("results", [{}])
        hits = rows[0].get("hits", []) if rows else []
        # Sliced here rather than trusted to `top_k`: the window the caller
        # asked for is this backend's contract, not the service's.
        return [
            self._normalize_hit(hit, index + 1)
            for index, hit in enumerate(hits[:depth])
            if index >= offset
        ]

    def _normalize_hit(self, hit: dict, rank: int) -> SearchHit:
        fields, body = parse_document_frontmatter(str(hit.get("snippet", "")))
        title = fields.get("title", "")
        return SearchHit(
            ref="",
            backend=self.name,
            docid=str(hit["docid"]),
            title=title,
            date=fields.get("date") or None,
            snippet=_strip_repeated_title(body, title),
            score=hit.get("score"),
            rank=int(hit.get("rank", rank)),
            # Everything else the header declared -- author, year, whatever a
            # future corpus adds -- stays reachable without a schema change.
            metadata={key: value for key, value in fields.items() if key not in {"title", "date"}},
        )

    async def content(
        self,
        hits: list[SearchHit],
        *,
        query: str | None = None,
    ) -> list[ContentSnippet]:
        del query

        async def fetch(client: httpx.AsyncClient, hit: SearchHit) -> ContentSnippet:
            metadata: dict[str, object] = {"docid": hit.docid, "backend": self.name}
            try:
                async with self._fetch_gate:
                    response = await client.post(
                        urljoin(self.base_url, "get_document"),
                        json={"docid": hit.docid},
                    )
                response.raise_for_status()
                payload = response.json()
            except Exception as exc:
                # One unreadable document must not take the other forty-nine
                # with it: the program asked for a batch and can act on a
                # partial one, but not on an exception.
                metadata["fetch_error"] = f"{type(exc).__name__}: {exc}"
                return ContentSnippet(ref=hit.ref, text="", title=hit.title, metadata=metadata)
            text = str(payload.get("text", ""))
            fields, _ = parse_document_frontmatter(text)
            if date := hit.date or fields.get("date"):
                metadata["date"] = date
            return ContentSnippet(
                ref=hit.ref,
                # The header is left in the text on purpose: it is part of the
                # document, and `content.read` addresses documents by line
                # number, so silently deleting lines here would shift every
                # offset a program computed from a `grep`.
                text=text,
                title=hit.title or fields.get("title", ""),
                metadata=metadata,
            )

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            return list(await asyncio.gather(*(fetch(client, hit) for hit in hits)))
