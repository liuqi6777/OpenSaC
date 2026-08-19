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


class CapabilityFailure(SubscriptableModel):
    """One failed item in a batch capability result.

    ``attempts`` counts transport attempts made by the broker. Input rejected
    before reaching a provider therefore has zero attempts.
    """

    code: str
    message: str
    retryable: bool
    attempts: int = Field(ge=0)
    provider_status: int | None = Field(default=None, ge=100, le=599)
    retry_after_seconds: float | None = Field(default=None, ge=0.0)


class SearchBatch(SubscriptableModel):
    model_config = ConfigDict(extra="forbid")

    query: str
    hits: list[SearchHit] = Field(default_factory=list)
    failure: CapabilityFailure | None = None
    request: SearchRequestInfo | None = None

    @model_validator(mode="after")
    def _validate_failure(self) -> Self:
        if self.failure is not None and self.hits:
            raise ValueError("a failed search batch cannot contain hits")
        return self


class CandidateSource(SubscriptableModel):
    batch_index: int
    query: str
    backend: str
    rank: int
    score: float | None = None


class SearchCandidate(SearchHit):
    sources: list[CandidateSource] = Field(default_factory=list)
    fused_score: float
    fused_rank: int


class FusionBatchError(SubscriptableModel):
    batch_index: int
    query: str
    failure: CapabilityFailure


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
    locator_error: EvidenceLocatorError | None = None
    failure: CapabilityFailure | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_failure(self) -> Self:
        if self.failure is not None and (
            self.text or self.locator is not None or self.locator_error is not None
        ):
            raise ValueError(
                "a failed content row must have empty text and no locator state"
            )
        return self


class ContentMatch(SubscriptableModel):
    """One line of a document that matched a pattern, with its coordinates.

    ``line`` is 1-indexed and is the same coordinate ``content.read`` takes as
    ``offset``, so locating and reading compose without arithmetic:

        report = sdk.content.grep_report(refs, r"born in \\d{4}")
        match = report.matches[0]
        window = sdk.content.read([match.ref], offset=max(1, match.line - 5))
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
    locator_error: EvidenceLocatorError | None = None
    # Populated by grep_report so duplicate input refs remain distinguishable.
    input_index: int | None = Field(default=None, ge=0)


class ContentFailure(SubscriptableModel):
    """A ref that could not be fetched while other content work succeeded."""

    input_index: int = Field(ge=0)
    ref: str
    failure: CapabilityFailure


class PassageCoordinates(SubscriptableModel):
    """Exact half-open coordinates of a passage in normalized document text.

    Lines are 1-indexed. Character offsets are 0-indexed within their
    respective lines, and ``end_character`` is exclusive.
    """

    model_config = ConfigDict(extra="forbid")

    start_line: int = Field(ge=1)
    start_character: int = Field(ge=0)
    end_line: int = Field(ge=1)
    end_character: int = Field(ge=0)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        start = (self.start_line, self.start_character)
        end = (self.end_line, self.end_character)
        if end <= start:
            raise ValueError("passage coordinates must describe a non-empty range")
        return self


class ContentPassage(SubscriptableModel):
    """One globally ranked passage selected from a caller-authorized ref set."""

    model_config = ConfigDict(extra="forbid")

    ref: str = Field(min_length=1)
    title: str = ""
    url: str | None = None
    date: str | None = None
    text: str = Field(min_length=1)
    coordinates: PassageCoordinates
    rank: int = Field(ge=1)
    score: float = Field(allow_inf_nan=False)
    ranker: str = Field(min_length=1)
    locator: EvidenceLocator | None = None
    locator_error: EvidenceLocatorError | None = None


class ContentPassageReport(SubscriptableModel):
    """Ranked passages plus typed fetch failures for the requested ref set."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1)
    passages: list[ContentPassage] = Field(default_factory=list)
    failures: list[ContentFailure] = Field(default_factory=list)
    input_count: int = Field(ge=0)
    unique_ref_count: int = Field(ge=0)

    @model_validator(mode="after")
    def _validate_counts(self) -> Self:
        if self.unique_ref_count > self.input_count:
            raise ValueError("unique_ref_count cannot exceed input_count")
        return self


class ContentGrepReport(SubscriptableModel):
    """Matches plus refs that grep could not inspect."""

    matches: list[ContentMatch] = Field(default_factory=list)
    failures: list[ContentFailure] = Field(default_factory=list)
    input_count: int = Field(ge=0)


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

    id: str = Field(min_length=1, max_length=128)
    ref: str = Field(min_length=1, max_length=256)
    kind: Literal["selected_passage"]


class EvidenceLocatorError(SubscriptableModel):
    """Why a returned passage could not receive a new trusted locator."""

    code: str
    message: str
    retryable: bool = False


class CitationRequest(SubscriptableModel):
    model_config = ConfigDict(extra="forbid")

    ref: str
    locator: EvidenceLocator | None = None

    @model_validator(mode="after")
    def _reject_explicit_null_locator(self) -> Self:
        if "locator" in self.model_fields_set and self.locator is None:
            raise ValueError(
                "locator must be omitted for a legacy search-preview citation; "
                "explicit null is not a valid evidence locator"
            )
        return self


class RpcRequest(BaseModel):
    method: str
    params: dict[str, Any] = Field(default_factory=dict)


class RpcError(BaseModel):
    code: str
    message: str
    retryable: bool
    attempts: int | None = Field(default=None, ge=0)
    provider_status: int | None = Field(default=None, ge=100, le=599)
    retry_after_seconds: float | None = Field(default=None, ge=0.0)


class RpcResponse(BaseModel):
    ok: bool
    result: Any = None
    # Accept the pre-v1 string form so a newer SDK can still explain an error
    # from an old broker. Contract-v1 brokers always send ``RpcError``.
    error: RpcError | str | None = None


class SubmittedOutput(BaseModel):
    output: Any
    citations: list[dict[str, Any]] = Field(default_factory=list)
