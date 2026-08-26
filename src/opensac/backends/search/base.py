"""Contracts implemented by broker-facing search backends."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


class RetrievalMetadata(BaseModel):
    """Retrieval and scoring semantics reported by a search backend."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    mode: str | None = None
    result_mode: str | None = None
    score_name: str | None = None
    higher_is_better: bool | None = None
    comparable_across_queries: bool | None = None


class SearchHit(BaseModel):
    """One provider search hit before or after broker source admission."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    source: str = ""
    backend: str
    title: str = ""
    url: str | None = None
    docid: str | None = None
    domain: str | None = None
    date: str | None = None
    snippet: str = ""
    score: float | None = None
    rank: int
    retrieval: RetrievalMetadata | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class SearchBatch(BaseModel):
    """One successful item returned by a batch-capable search backend."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    query: str
    hits: list[SearchHit] = Field(default_factory=list)


class SearchBatchFailure(BaseModel):
    """One provider-declared item failure in an otherwise valid batch response."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    code: str
    message: str
    retryable: bool
    provider_status: int | None = None
    retry_after_seconds: float | None = None


type SearchBatchOutcome = SearchBatch | SearchBatchFailure


class SearchBackend(Protocol):
    """Search I/O adapter; its service binds execution policy outside the adapter."""

    name: str
    result_cacheable: bool
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
        # Depth into the ranking, not just a convenience for paging. A source is
        # admitted only for a hit a search actually returned, so `limit` is
        # simultaneously the visibility ceiling and the authorisation ceiling:
        # without an offset a program that is certain the answer sits at rank 15
        # has no way to reach it, however it rewrites its query. `rank` stays
        # the rank in the full result list, never the index within the window.
        offset: int = 0,
        domains: list[str] | None = None,
    ) -> list[SearchHit]: ...


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
    ) -> list[SearchBatchOutcome]: ...


@runtime_checkable
class ClosableSearchBackend(Protocol):
    async def aclose(self) -> None: ...
