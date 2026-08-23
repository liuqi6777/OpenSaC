"""Validated internal domain and wire payloads owned by the OpenSAC host."""

from __future__ import annotations

from collections import Counter
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
    # Filled by the broker when search admits the hit into a session. Backends
    # provide only their native URL/docid and never mint public addresses.
    source: str = ""
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
    source: str
    text: str
    url: str | None = None
    title: str = ""
    # Carried over from the hit this text came from, for the same reason
    # `SearchHit.date` exists. Without it a program that filters on time has to
    # keep the hits alongside the snippets and join them by source, and the shape
    # of the SDK is what suggests otherwise: a snippet that has `title` and
    # `url` but no `date` reads like an oversight, and a program written on
    # that assumption dies on `AttributeError` rather than missing a filter.
    date: str | None = None
    failure: CapabilityFailure | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_failure(self) -> Self:
        if self.failure is not None and self.text:
            raise ValueError("a failed content row must have empty text")
        return self


class ContentMatch(BaseModel):
    """One line of a document that matched a pattern, with its coordinates.

    ``line`` is 1-indexed and is the same coordinate ``content.read`` takes as
    ``offset``, so locating and reading compose without arithmetic:

        report = sdk.content.grep(sources, r"born in \\d{4}")
        match = report.matches[0]
        window = sdk.content.read(match.source, offset=max(1, match.line - 5))
    """

    model_config = ConfigDict(extra="forbid")

    source: str = Field(min_length=1, max_length=4_096)
    title: str = ""
    line: int = Field(ge=1)
    text: str
    # Empty unless `context` was requested. Kept as two lists rather than one
    # so a program can tell which side of the match a line came from.
    before: list[str] = Field(default_factory=list)
    after: list[str] = Field(default_factory=list)
    # Duplicate input sources remain distinguishable by their request position.
    input_index: int = Field(ge=0)


class ContentFailure(BaseModel):
    """A source that could not be fetched while other content work succeeded."""

    model_config = ConfigDict(extra="forbid")

    input_index: int = Field(ge=0)
    source: str = Field(min_length=1, max_length=4_096)
    failure: CapabilityFailure


class ContentReadWindow(BaseModel):
    """One independently sliced source window for ``content.read_many``."""

    model_config = ConfigDict(extra="forbid", strict=True)

    source: str = Field(min_length=1, max_length=4_096)
    offset: int = Field(default=1, ge=1)
    limit: int = Field(default=200, ge=1, le=5_000)
    max_chars: int = Field(default=100_000, ge=1, le=400_000)


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
    """One globally ranked passage selected from a caller-authorized source set."""

    model_config = ConfigDict(extra="forbid")

    source: str = Field(min_length=1, max_length=4_096)
    title: str = ""
    date: str | None = None
    text: str = Field(min_length=1)
    coordinates: PassageCoordinates
    rank: int = Field(ge=1)
    score: float = Field(allow_inf_nan=False)
    ranker: str = Field(min_length=1)


class ContentPassageReport(BaseModel):
    """Ranked passages plus typed fetch failures for the requested source set."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1)
    passages: list[ContentPassage] = Field(default_factory=list)
    failures: list[ContentFailure] = Field(default_factory=list)
    input_count: int = Field(ge=0)
    unique_source_count: int = Field(ge=0)

    @model_validator(mode="after")
    def _validate_counts(self) -> Self:
        if self.unique_source_count > self.input_count:
            raise ValueError("unique_source_count cannot exceed input_count")
        return self


class ContentGrepSourceResult(BaseModel):
    """The scan outcome for one input source."""

    model_config = ConfigDict(extra="forbid")

    input_index: int = Field(ge=0)
    source: str = Field(min_length=1, max_length=4_096)
    title: str = ""
    match_count: int = Field(ge=0)
    scan_complete: bool
    failure: CapabilityFailure | None = None


class ContentGrepReport(BaseModel):
    """Flat matches plus complete, input-aligned source scan status."""

    model_config = ConfigDict(extra="forbid")

    pattern: str = Field(min_length=1, max_length=4_096)
    mode: Literal["regex", "literal"]
    case_sensitive: bool
    context: int = Field(ge=0, le=20)
    max_matches_per_source: int = Field(ge=1, le=200)
    matches: list[ContentMatch] = Field(default_factory=list)
    source_results: list[ContentGrepSourceResult] = Field(default_factory=list)
    input_count: int = Field(ge=0)

    @model_validator(mode="after")
    def _validate_alignment(self) -> Self:
        if len(self.source_results) != self.input_count:
            raise ValueError("source_results must align one-to-one with inputs")
        expected_indexes = list(range(self.input_count))
        if [row.input_index for row in self.source_results] != expected_indexes:
            raise ValueError("source_results input indexes must be contiguous and ordered")

        counts = Counter(match.input_index for match in self.matches)
        for match in self.matches:
            if match.input_index >= self.input_count:
                raise ValueError("match input_index is outside the input range")
            source_result = self.source_results[match.input_index]
            if source_result.failure is not None:
                raise ValueError("a failed source cannot contain matches")
            if match.source != source_result.source:
                raise ValueError("match source must equal its source result")

        for row in self.source_results:
            if row.match_count != counts[row.input_index]:
                raise ValueError("source result match_count does not match flat matches")
            if row.failure is not None:
                if row.match_count or row.scan_complete:
                    raise ValueError("a failed source must be incomplete with zero matches")
            elif not row.scan_complete and row.match_count != self.max_matches_per_source:
                raise ValueError("an incomplete scan must have reached the per-source limit")
        return self


class ExtractionRow(BaseModel):
    """One input-aligned structured extraction result."""

    model_config = ConfigDict(extra="forbid")

    index: int = Field(ge=0)
    data: dict[str, Any] | None = None
    failure: OperationError | None = None
    attempts: int = Field(ge=1)

    @model_validator(mode="after")
    def _validate_result(self) -> Self:
        if (self.data is None) == (self.failure is None):
            raise ValueError("exactly one of data or failure must be present")
        return self


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
