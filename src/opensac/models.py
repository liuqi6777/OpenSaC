from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(UTC)


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RunLimits(BaseModel):
    max_turns: int = Field(default=8, ge=1, le=50)
    max_search_calls: int = Field(default=200, ge=1, le=5000)
    max_llm_calls: int = Field(default=30, ge=0, le=500)
    timeout_seconds: int = Field(default=300, ge=1, le=3600)


# Every capability the broker dispatches, in one place. The broker asserts its
# handler table against this tuple, so a capability cannot be added on one side
# only: a method missing here would be invisible to the session manifest (and
# therefore to the skill text), and a method listed here without a handler would
# be advertised to the model and then fail on first use.
CAPABILITY_METHODS: tuple[str, ...] = (
    "search.web",
    "search.local",
    "search.web_many",
    "search.local_many",
    "content.get_many",
    "content.snippets",
    "content.read",
    "content.grep",
    "citations.resolve",
    "llm.complete",
    "llm.complete_many",
    "llm.extract_many",
)

# method -> the params key holding its batch. Used to bound fan-out when
# batching is disabled, so the ablation lands on "may I fan out in one call"
# rather than on "does this method exist".
FANOUT_METHODS: dict[str, str] = {
    "search.web_many": "queries",
    "search.local_many": "queries",
    "llm.complete_many": "prompts",
    "llm.extract_many": "items",
}


class Mechanisms(BaseModel):
    """Which of Search-as-Code's bundled properties this session may use.

    SaC is argued for as one paradigm but is really several mechanisms sold
    together, and a bundle cannot be attributed: an end-to-end win says nothing
    about which property produced it. These switches exist so a study can turn
    exactly one off and re-measure. See docs/research-instrumentation.md 1.3.

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
        rather than from a hand-maintained constant (design.md 6.7).
        """
        return [method for method in CAPABILITY_METHODS if self.blocked_reason(method) is None]


class SessionCreate(BaseModel):
    backends: list[str] = Field(default_factory=lambda: ["web", "local"])
    limits: RunLimits = Field(default_factory=RunLimits)
    mechanisms: Mechanisms = Field(default_factory=Mechanisms)


class Session(BaseModel):
    id: str
    token: str
    backends: list[str]
    limits: RunLimits
    workspace: str
    mechanisms: Mechanisms = Field(default_factory=Mechanisms)
    created_at: datetime = Field(default_factory=utc_now)


class RunCreate(BaseModel):
    input: str = Field(min_length=1)
    model: str | None = None
    output_schema: dict[str, Any] | None = None
    include_trace: bool = False


class RunUsage(BaseModel):
    model_tokens: int = 0
    pipeline_model_tokens: int = 0
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
    llm_calls: int = 0
    sandbox_seconds: float = 0.0


class ExecCreate(BaseModel):
    """One turn of an externally driven Search as Code loop.

    `POST /v1/sessions/{id}/runs` hands the task to OpenSAC's own control
    model. `POST /v1/sessions/{id}/exec` instead lets a foreign agent harness
    be the control plane: the harness generates the program and OpenSAC only
    supplies the sandbox, the SDK, and the capability broker. Session state --
    the workspace filesystem and the search reference table -- persists across
    exec calls, so a program can serialize intermediate results in one turn and
    resolve their refs several turns later.
    """

    code: str = Field(min_length=1)
    include_trace: bool = False


class HitRecord(BaseModel):
    """One search result, reduced to what offline analysis needs.

    ``identity`` is the stable key the broker assigns
    (docs/research-instrumentation.md 1.2) -- enough to count duplicates across
    queries, measure effective fan-out, and follow a document through later
    ranking stages. It embeds the docid or canonical URL rather than a hash of
    them on purpose: hashing would still support both of those, but it would
    make the trace impossible to join against a corpus's qrels, and "did the
    agent ever surface the gold document" is the first question anyone asks of
    a failed run.

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
    error_type: str | None = None
    error: str | None = None
    # Only populated when the session disables context decoupling: the result
    # the program received, echoed back so the caller can put it in the control
    # model's context. Empty in every default run.
    result_payload: Any = None
    result_payload_truncated: bool = False


class ExecResult(BaseModel):
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False
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


class Run(BaseModel):
    id: str
    session_id: str
    status: RunStatus = RunStatus.QUEUED
    input: str
    model: str | None = None
    output_schema: dict[str, Any] | None = None
    include_trace: bool = False
    output: Any = None
    citations: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None
    usage: RunUsage = Field(default_factory=RunUsage)
    trace: list[dict[str, Any]] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


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
    limits: RunLimits
    mechanisms: Mechanisms
    # Derived from `mechanisms`, returned so a host can build its skill text from
    # what the session can actually reach instead of from a duplicated constant.
    capabilities: list[str]
    created_at: datetime


class PublicRun(BaseModel):
    id: str
    session_id: str
    status: RunStatus
    input: str
    output: Any = None
    citations: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None
    usage: RunUsage
    trace: list[dict[str, Any]] | None = None
    artifacts: list[str] = Field(default_factory=list)
    events_url: str
    created_at: datetime
    updated_at: datetime
