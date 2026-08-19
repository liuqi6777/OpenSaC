"""Validated internal domain and wire payloads owned by the OpenSAC host."""

from __future__ import annotations

from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RetrievalMetadata(BaseModel):
    """The retrieval and scoring semantics reported by a search backend."""

    mode: str | None = None
    result_mode: str | None = None
    score_name: str | None = None
    higher_is_better: bool | None = None
    comparable_across_queries: bool | None = None


class SearchHit(BaseModel):
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


class OperationError(BaseModel):
    """Stable error information embedded in a successful operation result."""

    code: str
    message: str
    retryable: bool = False


class CapabilityFailure(OperationError):
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


class SearchBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str
    hits: list[SearchHit] = Field(default_factory=list)
    failure: CapabilityFailure | None = None

    @model_validator(mode="after")
    def _validate_failure(self) -> Self:
        if self.failure is not None and self.hits:
            raise ValueError("a failed search batch cannot contain hits")
        return self


class ContentSnippet(BaseModel):
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
    locator_error: OperationError | None = None
    failure: CapabilityFailure | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_failure(self) -> Self:
        if self.failure is not None and (
            self.text or self.locator is not None or self.locator_error is not None
        ):
            raise ValueError("a failed content row must have empty text and no locator state")
        return self


class ContentMatch(BaseModel):
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
    locator_error: OperationError | None = None
    # Populated by grep_report so duplicate input refs remain distinguishable.
    input_index: int | None = Field(default=None, ge=0)


class ContentFailure(BaseModel):
    """A ref that could not be fetched while other content work succeeded."""

    input_index: int = Field(ge=0)
    ref: str
    failure: CapabilityFailure


class PassageCoordinates(BaseModel):
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


class ContentPassage(BaseModel):
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
    locator_error: OperationError | None = None


class ContentPassageReport(BaseModel):
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


class ContentGrepReport(BaseModel):
    """Matches plus refs that grep could not inspect."""

    matches: list[ContentMatch] = Field(default_factory=list)
    failures: list[ContentFailure] = Field(default_factory=list)
    input_count: int = Field(ge=0)


class EvidenceLocator(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=128)
    ref: str = Field(min_length=1, max_length=256)
    kind: Literal["selected_passage"]


class RpcRequest(BaseModel):
    method: str
    params: dict[str, Any] = Field(default_factory=dict)


class RpcError(OperationError):
    attempts: int | None = Field(default=None, ge=0)
    provider_status: int | None = Field(default=None, ge=100, le=599)
    retry_after_seconds: float | None = Field(default=None, ge=0.0)


class RpcResponse(BaseModel):
    ok: bool
    result: Any = None
    # Accept the pre-v1 string form so a newer SDK can still explain an error
    # from an old broker. Contract-v1 brokers always send ``RpcError``.
    error: RpcError | str | None = None
