"""Serializable capability trace records produced by the broker."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

__all__ = [
    "CapabilityEvent",
    "CoalescedRequestRecord",
    "DeduplicatedRequestRecord",
    "HitRecord",
    "ModelAttemptRecord",
    "PassageTraceRecord",
    "ProviderAttemptRecord",
]


class HitRecord(BaseModel):
    """One document an event touched, reduced to what offline analysis needs.

    ``identity`` is the stable key the broker assigns -- enough to count
    duplicates across queries, measure effective fan-out, and follow a document
    through later ranking stages. It embeds the docid or canonical URL rather
    than a hash of them on purpose: hashing would still support both of those,
    but it would make the trace impossible to join against a corpus's qrels, and
    "did the agent ever surface the gold document" is the first question anyone
    asks of a failed run.

    Recorded on ``content.*`` as well as on searches,
    because surfacing and opening are different events with different remedies:
    a gold document that never appeared is a query-formulation failure, and one
    that appeared and was never opened is a reading failure, and a trace with
    identities on only one side cannot tell them apart. On those events ``rank``
    is where the document first appeared, which is the only rank they have.

    What stays out is page text: no snippet, no title, no fetched content. The
    trace is written for every run and has to remain publishable, and a
    document's address is the same class of data as the query that found it,
    which this trace already records verbatim.

    Recorded from the first baseline onwards because it cannot be recovered:
    a run that did not log ranks can never be asked afterwards whether ranking
    was the bottleneck.
    """

    identity: str
    rank: int
    score: float | None = None
    # Populated for search fan-out so one trace can reconstruct which query
    # surfaced a candidate. Content events have no query index.
    query_index: int | None = None
    # Effective backend behaviour, not a caller-requested mode. Kept flat in
    # the trace so older analysis does not need the SDK model to read it.
    retrieval_mode: str | None = None
    # How this document entered the session registry. Rejected inputs have no
    # admission because they were never registered.
    admission: Literal["search", "direct_url"] | None = None


class ModelAttemptRecord(BaseModel):
    """One pipeline-model attempt, without prompts or generated content."""

    index: int
    phase: str
    status: str
    duration_seconds: float
    model_tokens: int = 0
    error_code: str | None = None


class PassageTraceRecord(BaseModel):
    """Ranked-passage metadata without retrieved document text."""

    identity: str
    ranker: str
    rank: int = Field(ge=1)
    score: float
    coordinates: dict[str, Any] = Field(default_factory=dict)
    passage_fingerprint: str


class ProviderAttemptRecord(BaseModel):
    """One backend attempt without request or response bodies."""

    request_id: str
    attempt_id: str
    provider: str
    component: str
    request_indexes: list[int] = Field(default_factory=list)
    attempt: int = Field(ge=1)
    status: str
    duration_seconds: float = 0.0
    queue_seconds: float = 0.0
    rate_limit_wait_seconds: float = 0.0
    backoff_before_seconds: float = 0.0
    error_code: str | None = None
    provider_status: int | None = None
    request_fingerprint: str
    response_fingerprint: str | None = None


class DeduplicatedRequestRecord(BaseModel):
    """Logical rows served by another row in the same capability call."""

    request_index: int
    leader_index: int
    request_fingerprint: str


class CoalescedRequestRecord(BaseModel):
    """Logical rows which waited on an already-running provider request."""

    request_id: str
    request_indexes: list[int] = Field(default_factory=list)
    request_fingerprint: str


class CapabilityEvent(BaseModel):
    """One serializable capability invocation and its bounded diagnostics."""

    sequence: int
    method: str
    status: str
    duration_seconds: float
    queries: list[str] = Field(default_factory=list)
    input_count: int = 0
    result_count: int = 0
    hits: list[HitRecord] = Field(default_factory=list)
    model_tokens: int = 0
    model_attempts: list[ModelAttemptRecord] = Field(default_factory=list)
    passage_records: list[PassageTraceRecord] = Field(default_factory=list)
    provider_attempts: list[ProviderAttemptRecord] = Field(default_factory=list)
    deduplicated_requests: list[DeduplicatedRequestRecord] = Field(default_factory=list)
    coalesced_requests: list[CoalescedRequestRecord] = Field(default_factory=list)
    provider_cache_hits: int = 0
    provider_cache_misses: int = 0
    error_type: str | None = None
    error: str | None = None
    # Only populated when the session disables context decoupling: the result
    # the program received, echoed back so the caller can put it in the control
    # model's context. Empty in every default run.
    result_payload: Any = None
    result_payload_truncated: bool = False
