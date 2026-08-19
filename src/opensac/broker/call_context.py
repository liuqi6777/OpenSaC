from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from opensac.models import (
    CapabilityEvent,
    CoalescedRequestRecord,
    DeduplicatedRequestRecord,
    EvidenceTraceRecord,
    HitRecord,
    ModelAttemptRecord,
    PassageTraceRecord,
    ProviderAttemptRecord,
)


@dataclass
class ProviderTraceBuffer:
    """Attempt sink shared with provider tasks that may outlive their caller."""

    records: list[ProviderAttemptRecord] = field(default_factory=list)
    event: CapabilityEvent | None = None

    def append(self, record: ProviderAttemptRecord) -> None:
        self.records.append(record)
        if self.event is not None:
            self.event.provider_attempts.append(record)

    def bind(self, event: CapabilityEvent) -> None:
        self.event = event


@dataclass
class CallContext:
    """Mutable trace state for one broker capability invocation."""

    session_token: str
    execution_id: str | None
    model_tokens: int = 0
    hits: list[HitRecord] = field(default_factory=list)
    model_attempts: list[ModelAttemptRecord] = field(default_factory=list)
    evidence_records: list[EvidenceTraceRecord] = field(default_factory=list)
    passage_records: list[PassageTraceRecord] = field(default_factory=list)
    provider_trace: ProviderTraceBuffer = field(default_factory=ProviderTraceBuffer)
    deduplicated_requests: list[DeduplicatedRequestRecord] = field(default_factory=list)
    coalesced_requests: list[CoalescedRequestRecord] = field(default_factory=list)

    @property
    def provider_attempts(self) -> list[ProviderAttemptRecord]:
        return self.provider_trace.records


_CURRENT_CALL: ContextVar[CallContext | None] = ContextVar(
    "opensac_broker_call_context",
    default=None,
)


@contextmanager
def call_scope(session_token: str, execution_id: str | None) -> Iterator[CallContext]:
    context = CallContext(session_token=session_token, execution_id=execution_id)
    token = _CURRENT_CALL.set(context)
    try:
        yield context
    finally:
        _CURRENT_CALL.reset(token)


def current_call() -> CallContext | None:
    return _CURRENT_CALL.get()


_ERROR_MESSAGE_CHARS = 400


def trace_error_message(exc: BaseException) -> str | None:
    """Return a bounded diagnostic suitable for persistent research traces."""

    message = str(exc).strip()
    if not message:
        return None
    if len(message) <= _ERROR_MESSAGE_CHARS:
        return message
    return message[:_ERROR_MESSAGE_CHARS] + "... [truncated]"
