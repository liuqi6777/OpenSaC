from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from opensac._contracts import SearchHit
from opensac.broker.policy import CapabilityPolicy
from opensac.models import CapabilityEvent, Mechanisms, Session


@dataclass(frozen=True)
class EvidenceRecord:
    identity: str
    kind: str
    text: str
    coordinates: dict[str, Any]
    document_fingerprint: str
    passage_fingerprint: str


@dataclass
class FlightGroup:
    """Transport work shared by one or more active provider request keys."""

    operation_id: str
    keys: set[str] = field(default_factory=set)
    entries: dict[str, FlightEntry] = field(default_factory=dict)
    task: asyncio.Task[None] | None = None


@dataclass
class FlightEntry:
    """One active, session-scoped provider result shared by capability calls."""

    future: asyncio.Future[Any]
    operation_id: str
    request_fingerprint: str
    group: FlightGroup
    waiters: int = 0


@dataclass(eq=False)
class FlightWaiter:
    """One capability call's attachment to one unique provider request key."""

    key: str
    entry: FlightEntry
    execution_id: str | None
    active: bool = True


@dataclass
class FlightAdmission:
    waiters: dict[str, FlightWaiter]
    new_groups: list[FlightGroup]


@dataclass
class BrokerSession:
    """Mutable broker-owned state scoped to one authorized session."""

    session: Session
    policy: CapabilityPolicy
    # Search is the only writer, so knowing a URL or docid does not authorize a fetch.
    documents_by_source: dict[str, SearchHit] = field(default_factory=dict)
    source_by_identity: dict[str, str] = field(default_factory=dict)
    identity_by_source: dict[str, str] = field(default_factory=dict)
    # Only successful fetches are cached. Failures remain retryable within the session.
    content_cache: dict[str, dict[str, Any]] = field(default_factory=dict)
    content_cache_bytes: int = 0
    evidence: dict[str, EvidenceRecord] = field(default_factory=dict)
    evidence_passage_bytes: int = 0
    flights: dict[str, FlightEntry] = field(default_factory=dict)
    flight_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    flight_waiters_by_execution: dict[str, set[FlightWaiter]] = field(default_factory=dict)
    traces: dict[str, list[CapabilityEvent]] = field(default_factory=dict)
    trace_sequence: int = 0

    @property
    def mechanisms(self) -> Mechanisms:
        return self.session.mechanisms

    def next_trace_sequence(self) -> int:
        self.trace_sequence += 1
        return self.trace_sequence

    def cache_content(self, identity: str, row: dict[str, Any], budget: int) -> None:
        """Cache a successful document for this session without eviction churn."""

        text = row.get("text") or ""
        if not identity or identity in self.content_cache or row.get("failure") is not None:
            return
        size = len(str(text).encode("utf-8"))
        if self.content_cache_bytes + size > budget:
            return
        self.content_cache[identity] = row
        self.content_cache_bytes += size

    def remember(self, hit: SearchHit, *, identity: str, candidate_source: str) -> str:
        """Admit one document and return its stable public source."""

        source = self.source_by_identity.get(identity)
        if source is not None:
            return source

        source = candidate_source
        previous_identity = self.identity_by_source.get(source)
        if previous_identity is not None and previous_identity != identity:
            raise ValueError("Search backend returned colliding document sources")
        hit.source = source
        self.source_by_identity[identity] = source
        self.identity_by_source[source] = identity
        self.documents_by_source[source] = hit
        return source
