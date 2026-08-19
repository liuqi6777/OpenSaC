from __future__ import annotations

from typing import Protocol, runtime_checkable

from opensac._contracts import ContentSnippet, SearchBatch, SearchHit


class SearchBackend(Protocol):
    name: str
    # Opaque process-wide governor identity. It incorporates the effective
    # endpoint and credential without exposing either in trace records.
    provider_identity: str

    # Deployment facts a caller must be able to read off the backend rather than
    # infer from its name. The broker enforces both, so that the one search
    # capability stays backend-neutral in its *name* while staying honest about
    # what this particular deployment can do: a parameter a backend cannot
    # honour is refused, never quietly dropped.
    supports_domains: bool
    # Deepest rank reachable, or None for a backend with no ceiling.
    max_depth: int | None

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

    async def fetch(
        self,
        hit: SearchHit,
        *,
        query: str | None = None,
    ) -> ContentSnippet:
        """Fetch and normalize one document with exactly one transport call.

        Batch alignment and partial-failure rows belong to the broker. Keeping
        this operation atomic lets the provider runtime account, retry and
        cancel each real document request without a hidden adapter semaphore or
        exception-swallowing gather.
        """
        ...


@runtime_checkable
class BatchSearchBackend(Protocol):
    """Optional backend fast path for one transport-level batch request.

    The broker continues to support :class:`SearchBackend` implementations that
    only expose ``search``.  Backends implementing this protocol can avoid one
    HTTP round trip per query and, more importantly for dense retrieval, let the
    downstream service encode the queries as one model batch.
    """

    async def search_many(
        self,
        queries: list[str],
        *,
        limit: int,
        offset: int = 0,
        domains: list[str] | None = None,
    ) -> list[SearchBatch]: ...


@runtime_checkable
class ClosableSearchBackend(Protocol):
    async def aclose(self) -> None: ...
