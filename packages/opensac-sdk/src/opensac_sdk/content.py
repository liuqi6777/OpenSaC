from __future__ import annotations

from .models import ContentGrepReport, ContentMatch, ContentPassageReport, ContentSnippet
from .transport import UnixSocketTransport


class ContentResource:
    """Retrieve, locate, and inspect evidence from authorized documents.

    New programs normally compose `passages`, `grep_report`, and `read`.
    `passages` ranks evidence across documents, `grep_report` locates exact
    strings without hiding fetch failures, and `read` expands a line window.
    Line numbers are 1-indexed and shared by all three operations.

    `get_many` is an advanced whole-document operation. `snippets` and `grep`
    remain available for 0.5 compatibility, but new code should use `passages`
    and `grep_report` respectively.

    Documents are cached for the session after their first retrieval.
    """

    def __init__(self, transport: UnixSocketTransport) -> None:
        self._transport = transport

    def get_many(self, refs: list[str]) -> list[ContentSnippet]:
        """Fetch whole documents in input order; prefer narrower core operations."""
        result = self._transport.call("content.get_many", {"refs": refs})
        return [ContentSnippet.model_validate(item) for item in result]

    def read(
        self,
        refs: list[str],
        *,
        offset: int = 1,
        limit: int = 200,
        max_chars: int = 100_000,
    ) -> list[ContentSnippet]:
        """The same line window of each document, 1-indexed.

        `metadata` carries `start_line`, `end_line`, `total_lines`, and
        `next_offset` (None at the end), so scrolling one document is a loop
        and heading a whole candidate list is one call.

        `max_chars` bounds the window as well as `limit`, because a line is a
        sentence in some corpora and a whole section in scraped web pages.
        """
        result = self._transport.call(
            "content.read",
            {"refs": refs, "offset": offset, "limit": limit, "max_chars": max_chars},
        )
        return [ContentSnippet.model_validate(item) for item in result]

    def grep(
        self,
        refs: list[str],
        pattern: str,
        *,
        context: int = 0,
        max_matches_per_ref: int = 20,
    ) -> list[ContentMatch]:
        """Legacy match-only view; prefer `grep_report` for typed fetch failures.

        Case-insensitive; a pattern that is not valid regex is searched
        literally rather than raising.
        """
        result = self._transport.call(
            "content.grep",
            {
                "refs": refs,
                "pattern": pattern,
                "context": context,
                "max_matches_per_ref": max_matches_per_ref,
            },
        )
        return [ContentMatch.model_validate(item) for item in result]

    def grep_report(
        self,
        refs: list[str],
        pattern: str,
        *,
        context: int = 0,
        max_matches_per_ref: int = 20,
    ) -> ContentGrepReport:
        """Matching lines plus a typed row for every ref that could not be read."""
        result = self._transport.call(
            "content.grep_report",
            {
                "refs": refs,
                "pattern": pattern,
                "context": context,
                "max_matches_per_ref": max_matches_per_ref,
            },
        )
        return ContentGrepReport.model_validate(result)

    def snippets(
        self,
        query: str,
        refs: list[str],
        *,
        max_tokens: int = 4000,
        max_tokens_per_page: int = 1000,
    ) -> list[ContentSnippet]:
        """Legacy page-local windows; prefer globally ranked `passages`."""
        result = self._transport.call(
            "content.snippets",
            {
                "query": query,
                "refs": refs,
                "max_tokens": max_tokens,
                "max_tokens_per_page": max_tokens_per_page,
            },
        )
        return [ContentSnippet.model_validate(item) for item in result]

    def passages(
        self,
        query: str,
        refs: list[str],
        *,
        limit: int = 20,
        max_per_ref: int = 3,
    ) -> ContentPassageReport:
        """Globally rank citeable passages across an authorized document set.

        Scores are comparable only within one report. The deployment chooses
        the ranker; generated programs choose only the evidence query, input
        refs, result depth, and per-document diversity cap.
        """
        result = self._transport.call(
            "content.passages",
            {
                "query": query,
                "refs": refs,
                "limit": limit,
                "max_per_ref": max_per_ref,
            },
        )
        return ContentPassageReport.model_validate(result)
