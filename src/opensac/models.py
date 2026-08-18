from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, Field, model_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


class ExecRecordStatus(StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"


class ResourceBudget(BaseModel):
    """Optional hard ceilings for an externally-driven environment session.

    ``None`` means unbounded and preserves the historical evaluation behaviour.
    Discrete resources are reserved before their side effect.  Output tokens are
    reservations rather than observed usage, so concurrent fan-out cannot spend
    more than the caller authorised.
    """

    max_exec_calls: int | None = Field(default=None, ge=0)
    max_search_queries: int | None = Field(default=None, ge=0)
    max_content_fetches: int | None = Field(default=None, ge=0)
    max_pipeline_llm_calls: int | None = Field(default=None, ge=0)
    max_pipeline_output_tokens: int | None = Field(default=None, ge=0)
    max_sandbox_seconds: float | None = Field(default=None, ge=0.0)
    max_workspace_bytes: int | None = Field(default=None, ge=0)

# Every capability the broker dispatches, in one place. The broker asserts its
# handler table against this tuple, so a capability cannot be added on one side
# only: a method missing here would be invisible to the session manifest (and
# therefore to the skill text), and a method listed here without a handler would
# be advertised to the model and then fail on first use.
CAPABILITY_METHODS: tuple[str, ...] = (
    # One search, whichever backend this session was deployed against. The
    # backend is a deployment fact, not something a program chooses: it reaches
    # exactly one corpus, and naming that corpus in the method would make the
    # generated program unportable across arms and the skill text differ by
    # backend for no gain. Where a backend genuinely differs -- a domain filter,
    # a depth ceiling -- it differs in a *parameter*, which the broker refuses
    # explicitly rather than absorbing it. `hit.backend` still carries
    # provenance, so nothing downstream loses track of where a document came
    # from.
    "search.query",
    "search.query_many",
    "content.get_many",
    "content.snippets",
    "content.read",
    "content.grep",
    "content.grep_report",
    "citations.resolve",
    # The program's own budget. Without it the only place the remaining quota
    # appears is the observation the host renders for the control model, so the
    # code that decides whether to search again cannot see what searching has
    # already cost.
    "session.usage",
    "llm.complete",
    "llm.complete_many",
    "llm.extract_many",
)

# method -> the params key holding its batch. Used to bound fan-out when
# batching is disabled, so the ablation lands on "may I fan out in one call"
# rather than on "does this method exist".
FANOUT_METHODS: dict[str, str] = {
    "search.query_many": "queries",
    "llm.complete_many": "prompts",
    "llm.extract_many": "items",
}


class Mechanisms(BaseModel):
    """Which of Search-as-Code's bundled properties this session may use.

    SaC is argued for as one paradigm but is really several mechanisms sold
    together, and a bundle cannot be attributed: an end-to-end win says nothing
    about which property produced it. These switches exist so a study can turn
    exactly one off and re-measure.

    Every field defaults to True, so an omitted object -- or an omitted field --
    reproduces current behaviour byte for byte and keeps finished runs
    comparable. They are recorded in ``session.json``, which is what makes a
    run's arm recoverable after the fact.
    """

    # `*_many` fan-out. Disabled, a batch method still exists but accepts one
    # item, so the program has to loop. Removing the methods outright would
    # also remove structured extraction (`llm.extract_many` has no singular
    # form) and the arm would measure two things at once.
    batching: bool = True
    # The workspace filesystem surviving across `/exec` calls. Governs
    # agent-authored state only; the broker's reference table is a capability
    # handle rather than something the program wrote, and dropping it would
    # measure "can you search again" instead of "can you take notes".
    persistence: bool = True
    # `llm.*` as a subroutine of the generated program.
    llm_subroutine: bool = True
    # Keeping intermediate results out of the control model's context. Disabled,
    # every capability result is echoed back through the trace so the caller can
    # put it in the conversation: same interface, same expressiveness, results
    # back in token space. This is the arm that separates "SaC helps because you
    # can orchestrate" from "SaC helps because the middle never reaches context".
    context_decoupling: bool = True

    def blocked_reason(self, method: str) -> str | None:
        """Why this session may not call ``method`` at all, or None."""
        if not self.llm_subroutine and method.startswith("llm."):
            return (
                f"Capability '{method}' is disabled for this session: pipeline LLM "
                "subroutines are turned off. Do the work in plain Python, or report "
                "what you found without a model call."
            )
        return None

    def fanout_reason(self, method: str, params: dict[str, Any]) -> str | None:
        """Why this session may not fan ``method`` out this wide, or None."""
        if self.batching:
            return None
        key = FANOUT_METHODS.get(method)
        if key is None:
            return None
        size = len(params.get(key) or [])
        if size <= 1:
            return None
        return (
            f"Batching is disabled for this session: '{method}' accepts at most one "
            f"item in '{key}', got {size}. Call it once per item in a loop."
        )

    def capabilities(self) -> list[str]:
        """The methods a program may call, for the session manifest.

        A skill that names a capability the session cannot reach costs the model
        a turn to discover, so the host builds its primitive list from this
        rather than from a hand-maintained constant.
        """
        return [method for method in CAPABILITY_METHODS if self.blocked_reason(method) is None]


class SessionCreate(BaseModel):
    mechanisms: Mechanisms = Field(default_factory=Mechanisms)
    request_id: str | None = Field(default=None, min_length=1, max_length=256)
    lease_seconds: float | None = Field(default=None, gt=0.0, le=86_400.0)
    budget: ResourceBudget = Field(default_factory=ResourceBudget)

    @model_validator(mode="before")
    @classmethod
    def reject_backend_override(cls, data: Any) -> Any:
        if isinstance(data, dict) and "backends" in data:
            raise ValueError(
                "Search backend is configured when OpenSAC starts; remove "
                "'backends' from the session request."
            )
        return data


class Session(BaseModel):
    id: str
    token: str
    backends: list[str]
    workspace: str
    mechanisms: Mechanisms = Field(default_factory=Mechanisms)
    request_id: str | None = None
    request_hash: str | None = None
    worker_id: str = ""
    worker_epoch: str = ""
    lease_seconds: float | None = None
    lease_expires_at: datetime | None = None
    budget: ResourceBudget = Field(default_factory=ResourceBudget)
    usage: RunUsage = Field(default_factory=lambda: RunUsage())
    terminal_reason: str | None = None
    environment: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    # Updated by every operation that keeps the session alive. Kept beside the
    # session rather than in process memory so a restarted reaper does not make
    # every old workspace immortal (or immediately reap a session that was just
    # used by the previous process).
    last_access: datetime = Field(default_factory=utc_now)
    # Persisted before DELETE waits for an in-flight execution. A new process
    # that starts in the middle of cleanup therefore continues closing instead
    # of admitting new work into a workspace whose owner already deleted it.
    closing: bool = False


class RunUsage(BaseModel):
    exec_calls: int = 0
    pipeline_model_tokens: int = 0
    # Reserved maximum completion tokens.  Kept separate from actual provider
    # usage because it is the value a hard fan-out budget can enforce up front.
    pipeline_output_tokens_reserved: int = 0
    search_calls: int = 0
    # Documents the program asked for, counting every request including the ones
    # a session cache served.
    content_fetches: int = 0
    # Documents actually retrieved from a backend. The pair is deliberate: one
    # number alone cannot answer both "how much fetching did the program do"
    # (a property of its strategy) and "how much load did it cause" (a property
    # of the infrastructure), and a cache makes those two diverge. Reporting
    # only the first would hide the saving; only the second would make a program
    # look like it stopped reading.
    content_backend_fetches: int = 0
    # Real transport attempts, distinct from the logical operations above. A
    # local search_many HTTP request counts once even when it carries many
    # queries; retries add attempts without adding logical calls.
    search_provider_attempts: int = 0
    content_provider_attempts: int = 0
    provider_retries: int = 0
    intra_call_deduplicated_items: int = 0
    provider_coalesced_requests: int = 0
    provider_queue_seconds: float = 0.0
    provider_rate_limit_wait_seconds: float = 0.0
    provider_backoff_seconds: float = 0.0
    # Trusted passage state held by the broker for this session. These are
    # measurements, not agent-spend budgets, so they intentionally have no
    # ResourceBudget counterpart.
    evidence_records: int = 0
    evidence_passage_bytes: int = 0
    llm_calls: int = 0
    sandbox_seconds: float = 0.0
    workspace_bytes: int = 0


_BUDGET_USAGE_FIELDS: dict[str, str] = {
    "max_exec_calls": "exec_calls",
    "max_search_queries": "search_calls",
    "max_content_fetches": "content_fetches",
    "max_pipeline_llm_calls": "llm_calls",
    "max_pipeline_output_tokens": "pipeline_output_tokens_reserved",
    "max_sandbox_seconds": "sandbox_seconds",
    "max_workspace_bytes": "workspace_bytes",
}


def budget_remaining(
    budget: ResourceBudget,
    usage: RunUsage,
) -> dict[str, float | int | None]:
    remaining: dict[str, float | int | None] = {}
    for budget_field, usage_field in _BUDGET_USAGE_FIELDS.items():
        ceiling = getattr(budget, budget_field)
        remaining[budget_field] = (
            None if ceiling is None else max(ceiling - getattr(usage, usage_field), 0)
        )
    return remaining


class SessionTombstone(BaseModel):
    session_id: str
    reason: str
    request_id: str | None = None
    request_hash: str | None = None
    worker_id: str = ""
    worker_epoch: str = ""
    deleted_at: datetime = Field(default_factory=utc_now)


class ExecCreate(BaseModel):
    """One turn of an externally driven Search as Code loop.

    The agent harness is the control plane: it generates the program and
    OpenSAC supplies the sandbox, SDK, and capability broker. Session state --
    the workspace filesystem and search reference table -- persists across exec
    calls, so a program can serialize intermediate results in one turn and
    resolve their refs several turns later.
    """

    code: str = Field(min_length=1)
    include_trace: bool = False
    # A caller-owned idempotency key. Omitted preserves the original behaviour:
    # every request is a new action. When present, the completed response is
    # durable and a retry of the same payload does not execute the program again.
    exec_id: str | None = Field(default=None, min_length=1, max_length=256)


class HitRecord(BaseModel):
    """One document an event touched, reduced to what offline analysis needs.

    ``identity`` is the stable key the broker assigns -- enough to count
    duplicates across queries, measure effective fan-out, and follow a document
    through later ranking stages. It embeds the docid or canonical URL rather
    than a hash of them on purpose: hashing would still support both of those,
    but it would make the trace impossible to join against a corpus's qrels, and
    "did the agent ever surface the gold document" is the first question anyone
    asks of a failed run.

    Recorded on ``content.*`` and ``citations.resolve`` as well as on searches,
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
    # surfaced a candidate. Content and citation events have no query index.
    query_index: int | None = None
    # Effective backend behaviour, not a caller-requested mode. Kept flat in
    # the trace so older analysis does not need the SDK model to read it.
    retrieval_mode: str | None = None


class ModelAttemptRecord(BaseModel):
    """One pipeline-model attempt, without prompts or generated content."""

    index: int
    phase: str
    status: str
    duration_seconds: float
    model_tokens: int = 0
    error_code: str | None = None


class EvidenceTraceRecord(BaseModel):
    """Evidence lifecycle metadata suitable for persistent research traces."""

    locator_id: str | None = None
    ref: str
    action: str
    status: str
    coordinates: dict[str, Any] = Field(default_factory=dict)
    document_fingerprint: str | None = None
    passage_fingerprint: str | None = None
    error_code: str | None = None


class ProviderAttemptRecord(BaseModel):
    """One real provider transport attempt without request or response bodies."""

    operation_id: str
    attempt_id: str
    provider: str
    operation: str
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
    """Logical rows which waited on an already-running provider operation."""

    operation_id: str
    request_indexes: list[int] = Field(default_factory=list)
    request_fingerprint: str


class CapabilityEvent(BaseModel):
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
    evidence_records: list[EvidenceTraceRecord] = Field(default_factory=list)
    provider_attempts: list[ProviderAttemptRecord] = Field(default_factory=list)
    deduplicated_requests: list[DeduplicatedRequestRecord] = Field(default_factory=list)
    coalesced_requests: list[CoalescedRequestRecord] = Field(default_factory=list)
    error_type: str | None = None
    error: str | None = None
    # Only populated when the session disables context decoupling: the result
    # the program received, echoed back so the caller can put it in the control
    # model's context. Empty in every default run.
    result_payload: Any = None
    result_payload_truncated: bool = False


class WorkspaceFile(BaseModel):
    """One file a program wrote, with its content, for the research record.

    Not part of `ExecResult`: this is read once at the end of a rollout, by the
    harness, on its way to archiving the run. Putting it in every execution
    result would push the workspace through the control model's observation on
    every turn, which is the one thing the architecture exists to avoid.
    """

    path: str
    bytes: int
    text: str
    truncated: bool = False


class WorkspaceSnapshot(BaseModel):
    files: list[WorkspaceFile] = Field(default_factory=list)
    # Files present but not returned, because the snapshot budget ran out
    # first. Reported rather than dropped: a snapshot that silently omitted
    # half the workspace would misrepresent what the program accumulated.
    omitted: list[str] = Field(default_factory=list)


class ExecResult(BaseModel):
    exec_id: str | None = None
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False
    output_limit_exceeded: bool = False
    succeeded: bool
    output: Any = None
    citations: list[dict[str, Any]] = Field(default_factory=list)
    # Set when the program never ran: rejected by the code validator, or the
    # sandbox container failed to start. Distinct from stderr, which carries
    # failures of a program that did run, because only the latter is something
    # the caller's control model can fix by rewriting its code.
    error: str | None = None
    # Cumulative for the session, not for this call: quotas are enforced across
    # the whole externally driven loop.
    usage: RunUsage
    artifacts: list[str] = Field(default_factory=list)
    trace: list[CapabilityEvent] = Field(default_factory=list)
    timings: dict[str, float] = Field(default_factory=dict)
    session_state: str = "active"
    terminal_reason: str | None = None
    budget_remaining: dict[str, float | int | None] = Field(default_factory=dict)


class ExecRecord(BaseModel):
    """One durable idempotent execution response.

    ``pending`` is written before entering the sandbox. If the process dies in
    the indeterminate interval before a completed response is atomically
    persisted, a restarted server refuses to silently execute the action again.
    """

    exec_id: str
    request_hash: str
    status: ExecRecordStatus = ExecRecordStatus.COMPLETED
    result: ExecResult | None = None
    completed_at: datetime | None = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        if self.status is ExecRecordStatus.PENDING:
            if self.result is not None or self.completed_at is not None:
                raise ValueError("A pending execution cannot have a completed result")
        elif self.result is None or self.completed_at is None:
            raise ValueError("A completed execution must have a result and completion time")
        return self


class ProgramRecord(BaseModel):
    """One generated program, archived outside the workspace.

    The program *is* the action under Search as Code, so a run that keeps only
    call counts has thrown away its primary observation. The archive is what
    makes it possible to ask afterwards whether models compose the low-level
    primitives or only ever reach for the high-level shorthand -- the claim the
    architecture rests on. It lives beside the workspace rather than in it, so
    it survives a session whose persistence is switched off and never reaches
    the control model as an artifact.
    """

    sequence: int
    path: str
    code: str
    exit_code: int | None = None
    timed_out: bool = False
    output_limit_exceeded: bool = False
    duration_seconds: float = 0.0
    error: str | None = None
    # Coarse and local: what this process can tell from the exit status alone.
    # The host classifies more finely using the capability trace; the two are
    # not meant to be identical.
    error_category: str | None = None
    # Lengths only. Program output can contain fetched page text, which does not
    # belong in an archive that is meant to be publishable.
    stdout_bytes: int = 0
    stderr_bytes: int = 0
    capability_calls: dict[str, int] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class PublicSession(BaseModel):
    id: str
    backends: list[str]
    mechanisms: Mechanisms
    # Derived from `mechanisms`, returned so a host can build its skill text from
    # what the session can actually reach instead of from a duplicated constant.
    capabilities: list[str]
    # Feature negotiation for transport behaviour. In particular, a host must
    # not retry `/exec` against an older server that silently ignores `exec_id`.
    features: list[str] = Field(default_factory=list)
    worker_id: str = ""
    worker_epoch: str = ""
    request_id: str | None = None
    lease_seconds: float | None = None
    lease_expires_at: datetime | None = None
    budget: ResourceBudget = Field(default_factory=ResourceBudget)
    usage: RunUsage = Field(default_factory=RunUsage)
    budget_remaining: dict[str, float | int | None] = Field(default_factory=dict)
    state: str = "active"
    terminal_reason: str | None = None
    environment: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    last_access: datetime
    closing: bool
