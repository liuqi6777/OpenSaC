from __future__ import annotations

import hashlib
import re
from typing import Any
from urllib.parse import urljoin

import httpx
from opensac_sdk.models import ContentSnippet, RetrievalMetadata, SearchBatch, SearchHit

from opensac.provider import ProviderRequestError, invalid_provider_response

# Full documents in the local corpus carry a YAML frontmatter header ahead of
# the body, and the body then repeats the title as its own first line:
#
#     ---
#     title: Royal Rumble (2020) - Wikipedia
#     date: 2018-11-19
#     ---
#     Royal Rumble (2020) - Wikipedia
#     The 2020 Royal Rumble was ...
#
# Deliberately not a YAML parser: the header is a flat `key: value` block, and
# taking a dependency to read a `/get_document` response would let a malformed
# document raise where an unparsed line should just be skipped. Search results
# are already shaped by the search server and never pass through this parser.
_FRONTMATTER_PATTERN = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*\n?", re.DOTALL)


def parse_document_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Split ``text`` into its frontmatter fields and the body below them.

    Mirrors ``DeepResearch-dev/src/local_search/snippets.py::_parse_document_frontmatter``
    so a full document renders with the same title and date as its search hit.
    Returns ``({}, text)`` unchanged when there is no header.
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


class LocalSearchBackend:
    name = "local"
    # The corpus has no notion of a site, and a filter that cannot be honoured
    # is refused by the broker rather than ignored here. It used to be silently
    # dropped, which handed a program filtering by domain an unfiltered result
    # set and no way to find out.
    supports_domains = False
    # A dense index over a fixed corpus: depth is bounded by the corpus, not by
    # a service policy, so there is no rank the backend refuses to reach.
    max_depth = None

    def __init__(
        self,
        base_url: str,
        timeout: float = 30.0,
        fetch_concurrency: int = 6,
    ) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        self.timeout = timeout
        # Kept as a constructor compatibility shim while concurrency ownership
        # moves to ProviderRuntime. The adapter itself performs one transport
        # operation per method and has no hidden admission gate.
        del fetch_concurrency
        self._client: httpx.AsyncClient | None = None

    @property
    def provider_identity(self) -> str:
        """Opaque limiter key that changes with the effective endpoint."""

        digest = hashlib.sha256(self.base_url.encode("utf-8")).hexdigest()
        return f"local:{digest}"

    def _http(self) -> httpx.AsyncClient:
        # Construct lazily so importing/configuring the service does not open a
        # connection pool. There is no await between the check and assignment,
        # so concurrent tasks on the event loop cannot create duplicate pools.
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def aclose(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    async def search(
        self,
        query: str,
        *,
        limit: int,
        offset: int = 0,
        domains: list[str] | None = None,
    ) -> list[SearchHit]:
        # Accepted and unused: the broker refuses a domain filter before it gets
        # here (`supports_domains = False`), so reaching this line with one set
        # is a broker bug rather than something to absorb quietly.
        del domains
        # The retrieval service has no offset parameter, so depth is reached by
        # asking for the whole prefix and discarding it. Wasteful on the wire
        # and cheap in practice (it is a local dense index), and it keeps the
        # backend honest about ranks: a hit's `rank` stays its rank in the full
        # result list rather than its position in the returned window, which is
        # what fusion and any offline qrels join need.
        depth = offset + limit
        response = await self._http().post(
            urljoin(self.base_url, "search"),
            json={"query": query, "top_k": depth},
        )
        response.raise_for_status()
        payload = self._json_object(response)
        retrieval = self._retrieval_metadata(payload)
        rows = payload.get("results")
        if not isinstance(rows, list) or len(rows) != 1:
            raise invalid_provider_response()
        first_row = rows[0]
        if not isinstance(first_row, dict):
            raise invalid_provider_response()
        hits = first_row.get("hits", [])
        if not isinstance(hits, list) or not all(isinstance(hit, dict) for hit in hits):
            raise invalid_provider_response()
        # Sliced here rather than trusted to `top_k`: the window the caller
        # asked for is this backend's contract, not the service's.
        try:
            return [
                self._normalize_hit(hit, index + 1, retrieval=retrieval)
                for index, hit in enumerate(hits[:depth])
                if index >= offset
            ]
        except (KeyError, TypeError, ValueError) as exc:
            raise invalid_provider_response() from exc

    async def search_many(
        self,
        queries: list[str],
        *,
        limit: int,
        offset: int = 0,
        domains: list[str] | None = None,
    ) -> list[SearchBatch]:
        """Search all queries in one retriever request, preserving their order."""
        del domains
        if not queries:
            return []
        depth = offset + limit
        response = await self._http().post(
            urljoin(self.base_url, "search_many"),
            json={"queries": queries, "top_k": depth},
        )
        response.raise_for_status()
        payload = self._json_object(response)
        retrieval = self._retrieval_metadata(payload)
        rows = payload.get("results")
        if not isinstance(rows, list) or len(rows) != len(queries):
            raise invalid_provider_response()

        batches: list[SearchBatch] = []
        for query, row in zip(queries, rows, strict=True):
            if not isinstance(row, dict):
                raise invalid_provider_response()
            returned_query = row.get("query")
            if returned_query != query:
                raise invalid_provider_response()
            raw_hits = row.get("hits", [])
            if not isinstance(raw_hits, list) or not all(
                isinstance(hit, dict) for hit in raw_hits
            ):
                raise invalid_provider_response()
            try:
                hits = [
                    self._normalize_hit(hit, index + 1, retrieval=retrieval)
                    for index, hit in enumerate(raw_hits[:depth])
                    if index >= offset
                ]
            except (KeyError, TypeError, ValueError) as exc:
                raise invalid_provider_response() from exc
            error = row.get("error")
            if error is not None and not isinstance(error, str):
                raise invalid_provider_response()
            try:
                batches.append(
                    SearchBatch(
                        query=query,
                        hits=hits,
                        error="Provider rejected one search item." if error else None,
                    )
                )
            except (TypeError, ValueError) as exc:
                raise invalid_provider_response() from exc
        return batches

    @staticmethod
    def _json_object(response: Any) -> dict[str, Any]:
        try:
            payload = response.json()
        except Exception as exc:
            raise invalid_provider_response() from exc
        if not isinstance(payload, dict):
            raise invalid_provider_response()
        return payload

    @staticmethod
    def _retrieval_metadata(payload: dict) -> RetrievalMetadata | None:
        mode = str(payload.get("backend", "") or "").strip() or None
        result_mode = str(payload.get("result_mode", "") or "").strip() or None
        if mode is None and result_mode is None:
            return None
        return RetrievalMetadata(
            mode=mode,
            result_mode=result_mode,
            score_name="backend_score" if mode else None,
            higher_is_better=True if mode else None,
            comparable_across_queries=False,
        )

    def _normalize_hit(
        self,
        hit: dict,
        rank: int,
        *,
        retrieval: RetrievalMetadata | None = None,
    ) -> SearchHit:
        # Snippet selection and document-field extraction belong to the search
        # server. In particular, query-aware snippets must arrive here intact:
        # parsing or trimming them again would make OpenSAC a second policy
        # owner and could silently erase the server-selected passage.
        known_fields = {"docid", "title", "date", "snippet", "score", "rank"}
        date = hit.get("date")
        return SearchHit(
            ref="",
            backend=self.name,
            docid=str(hit["docid"]),
            title=str(hit.get("title", "") or ""),
            date=str(date) if date is not None and date != "" else None,
            snippet=str(hit.get("snippet", "") or ""),
            score=hit.get("score"),
            rank=int(hit.get("rank", rank)),
            retrieval=retrieval,
            metadata={key: value for key, value in hit.items() if key not in known_fields},
        )

    async def fetch(
        self,
        hit: SearchHit,
        *,
        query: str | None = None,
    ) -> ContentSnippet:
        del query
        self.preflight_fetch(hit)
        response = await self._http().post(
            urljoin(self.base_url, "get_document"),
            json={"docid": hit.docid},
        )
        response.raise_for_status()
        payload = self._json_object(response)
        raw_text = payload.get("text")
        if not isinstance(raw_text, str):
            raise invalid_provider_response()
        fields, _ = parse_document_frontmatter(raw_text)
        metadata: dict[str, object] = {"docid": hit.docid, "backend": self.name}
        if date := hit.date or fields.get("date"):
            metadata["date"] = date
        return ContentSnippet(
            ref=hit.ref,
            # The header is left in the text on purpose: it is part of the
            # document, and `content.read` addresses documents by line number,
            # so deleting it here would shift offsets computed from a `grep`.
            text=raw_text,
            title=hit.title or fields.get("title", ""),
            metadata=metadata,
        )

    @staticmethod
    def preflight_fetch(hit: SearchHit) -> None:
        """Validate a local document handle before provider admission."""

        if not hit.docid:
            raise ProviderRequestError(
                "invalid_request",
                "Local search result has no document identifier.",
                retryable=False,
            )
