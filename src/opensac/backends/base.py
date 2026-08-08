from __future__ import annotations

from typing import Protocol

from opensac_sdk.models import ContentSnippet, SearchHit


class SearchBackend(Protocol):
    name: str

    async def search(
        self,
        query: str,
        *,
        limit: int,
        # Depth into the ranking, not just a convenience for paging. A ref is
        # only minted for a hit a search actually returned, so `limit` is
        # simultaneously the visibility ceiling and the authorisation ceiling:
        # without an offset a program that is certain the answer sits at rank 15
        # has no way to reach it, however it rewrites its query. `rank` stays
        # the rank in the full result list, never the index within the window.
        offset: int = 0,
        domains: list[str] | None = None,
    ) -> list[SearchHit]: ...

    async def content(
        self,
        hits: list[SearchHit],
        *,
        query: str | None = None,
    ) -> list[ContentSnippet]:
        """One snippet per hit, in the order given.

        A document that could not be retrieved comes back as a row with empty
        ``text`` and ``metadata["fetch_error"]`` describing why, never as a
        missing row. Dropping it silently makes a partial result look like a
        complete one: the program sees four pages where it asked for ten and
        has no way to learn which six are missing, and `content.read` on a page
        that failed to load becomes indistinguishable from one that is empty.
        """
        ...
