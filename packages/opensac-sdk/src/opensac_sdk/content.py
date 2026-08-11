from __future__ import annotations

from .models import ContentGrepReport, ContentMatch, ContentSnippet
from .transport import UnixSocketTransport


class ContentResource:
    """Four ways to read a document, from whole-page to a named line window.

    `get_many` and `snippets` both hand back a passage somebody else chose --
    the whole page, or the window a broker-side scorer liked best. `grep` and
    `read` are the pair that lets the program choose: locate a line, then read
    around it. Line numbers are 1-indexed and shared between the two, so a
    `ContentMatch.line` is a `read(offset=...)` with no arithmetic.

    A document retrieved once is cached for the rest of the session, so reading
    the same pool repeatedly is cheap after the first pass. `get_many`,
    `snippets`, and `read` return one row per requested document, in order; a
    page that could not be retrieved comes back with empty `text` and
    `metadata["fetch_error"]`. `grep` instead returns zero or more matches per
    document, so an absent ref means no line matched rather than a missing row.
    """

    def __init__(self, transport: UnixSocketTransport) -> None:
        self._transport = transport

    def get_many(self, refs: list[str]) -> list[ContentSnippet]:
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
        """Matching lines across many documents, with their line numbers.

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
