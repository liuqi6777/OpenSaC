from __future__ import annotations

from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

_MISSING = object()


class SubscriptableModel(BaseModel):
    """A result object that also answers to the access style of a mapping.

    Generated programs reach for ``hit["docid"]`` and ``snippet.get("text")``
    constantly, because almost every search API a model has ever read returns
    dictionaries. That prior does not go away when a docstring says otherwise,
    and the failure is not a graceful one: ``'SearchHit' object is not
    subscriptable`` aborts the program and costs the whole turn.

    This is not two representations tolerating each other. There is one type;
    it accepts two spellings of the same field read. Nothing here can reach a
    field the attribute form could not, so the shape of a result is unchanged
    and the two spellings can never disagree.

    Writes stay attribute-only on purpose. ``hit["ref"] = ...`` would be a
    program editing its own handle, and a handle it has edited is one the
    broker will refuse.
    """

    def __getitem__(self, key: str) -> Any:
        value = getattr(self, key, _MISSING)
        if value is _MISSING:
            # KeyError rather than AttributeError: the caller asked in the
            # mapping style and is likely wrapped in the matching except.
            raise KeyError(key)
        return value

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and key in type(self).model_fields

    def keys(self) -> Any:
        # Present so `dict(hit)` and `{**hit}` work, which is the third way a
        # program tries to treat a result as a mapping.
        return type(self).model_fields.keys()


class RetrievalMetadata(SubscriptableModel):
    """The retrieval and scoring semantics reported by a search backend."""

    mode: str | None = None
    result_mode: str | None = None
    score_name: str | None = None
    higher_is_better: bool | None = None
    comparable_across_queries: bool | None = None


class SearchRequestInfo(SubscriptableModel):
    """The request window that produced one search batch."""

    limit: int | None = None
    offset: int = 0
    domains: list[str] | None = None


class SearchHit(SubscriptableModel):
    ref: str
    backend: str
    title: str = ""
    url: str | None = None
    docid: str | None = None
    domain: str | None = None
    # Publication date as the backend reports it, un-normalised. First class
    # rather than a `metadata` key because a large share of retrieval tasks
    # constrain time ("released between 1980 and 2000", "as of 2023"), and a
    # program can only filter on a field it can guess the name of. Anything
    # else the backend knows stays in `metadata`.
    date: str | None = None
    snippet: str = ""
    score: float | None = None
    rank: int
    retrieval: RetrievalMetadata | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class SearchBatch(SubscriptableModel):
    query: str
    hits: list[SearchHit] = Field(default_factory=list)
    error: str | None = None
    request: SearchRequestInfo | None = None


class CandidateSource(SubscriptableModel):
    batch_index: int
    query: str
    backend: str
    rank: int
    score: float | None = None
    retrieval: RetrievalMetadata | None = None
    request: SearchRequestInfo | None = None


class SearchCandidate(SearchHit):
    sources: list[CandidateSource] = Field(default_factory=list)
    fused_score: float
    fused_rank: int


class FusionBatchError(SubscriptableModel):
    batch_index: int
    query: str
    error: str


class FusionResult(SubscriptableModel):
    candidates: list[SearchCandidate] = Field(default_factory=list)
    input_count: int
    unique_count: int
    duplicate_count: int
    batch_errors: list[FusionBatchError] = Field(default_factory=list)


class ContentSnippet(SubscriptableModel):
    ref: str
    text: str
    url: str | None = None
    title: str = ""
    # Carried over from the hit this text came from, for the same reason
    # `SearchHit.date` exists. Without it a program that filters on time has to
    # keep the hits alongside the snippets and join them by ref, and the shape
    # of the SDK is what suggests otherwise: a snippet that has `title` and
    # `url` but no `date` reads like an oversight, and a program written on
    # that assumption dies on `AttributeError` rather than missing a filter.
    date: str | None = None
    locator: EvidenceLocator | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ContentMatch(SubscriptableModel):
    """One line of a document that matched a pattern, with its coordinates.

    ``line`` is 1-indexed and is the same coordinate ``content.read`` takes as
    ``offset``, so locating and reading compose without arithmetic:

        matches = sdk.content.grep(refs, r"born in \\d{4}")
        window = sdk.content.read([matches[0].ref], offset=matches[0].line - 5)
    """

    ref: str
    docid: str | None = None
    url: str | None = None
    title: str = ""
    line: int
    text: str
    # Empty unless `context` was requested. Kept as two lists rather than one
    # so a program can tell which side of the match a line came from.
    before: list[str] = Field(default_factory=list)
    after: list[str] = Field(default_factory=list)
    locator: EvidenceLocator | None = None


class ExtractionError(SubscriptableModel):
    code: str
    message: str
    retryable: bool


class ExtractionResult(SubscriptableModel):
    index: int
    data: dict[str, Any] | None = None
    error: ExtractionError | None = None
    attempts: int = Field(ge=1)

    @model_validator(mode="after")
    def _has_data_or_error(self) -> Self:
        if (self.data is None) == (self.error is None):
            raise ValueError("exactly one of data or error must be set")
        return self


class EvidenceLocator(SubscriptableModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    ref: str
    kind: Literal["selected_passage"]


class CitationRequest(SubscriptableModel):
    model_config = ConfigDict(extra="forbid")

    ref: str
    locator: EvidenceLocator | None = None


class RpcRequest(BaseModel):
    method: str
    params: dict[str, Any] = Field(default_factory=dict)


class RpcError(BaseModel):
    code: str
    message: str
    retryable: bool


class RpcResponse(BaseModel):
    ok: bool
    result: Any = None
    # Accept the pre-v1 string form so a newer SDK can still explain an error
    # from an old broker. Contract-v1 brokers always send ``RpcError``.
    error: RpcError | str | None = None


class SubmittedOutput(BaseModel):
    output: Any
    citations: list[dict[str, Any]] = Field(default_factory=list)
