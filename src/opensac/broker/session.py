from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Literal

from opensac._contracts import SearchHit
from opensac.broker.policy import CapabilityPolicy
from opensac.models import CapabilityEvent, Mechanisms, Session


@dataclass
class DocumentRecord:
    """Broker-private identity and aliases for one admitted document."""

    document_id: str
    backend: str
    public_source: str
    fetch_url: str | None
    docid: str | None
    admission: Literal["search", "direct_url"]
    hit: SearchHit
    aliases: set[str] = field(default_factory=set)
    fetched: bool = False


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
    documents_by_id: dict[str, DocumentRecord] = field(default_factory=dict)
    document_id_by_alias: dict[str, str] = field(default_factory=dict)
    document_id_by_backend_identity: dict[str, str] = field(default_factory=dict)
    # Only successful fetches are cached. Failures remain retryable within the session.
    content_cache: dict[str, dict[str, Any]] = field(default_factory=dict)
    content_cache_bytes: int = 0
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

    def remember(
        self,
        hit: SearchHit,
        *,
        identity: str,
        candidate_source: str,
        admission: Literal["search", "direct_url"] = "search",
        aliases: set[str] | None = None,
    ) -> str:
        """Admit one document and return its stable public source."""

        document_id = self.document_id_by_backend_identity.get(identity, identity)
        record = self.documents_by_id.get(document_id)
        requested_aliases = {candidate_source, *(aliases or set())}
        if record is not None:
            for alias in requested_aliases:
                self._remember_alias(record, alias)
            hit.source = record.public_source
            return record.public_source

        for alias in requested_aliases:
            previous_id = self.document_id_by_alias.get(alias)
            if previous_id is not None and previous_id != document_id:
                raise ValueError("Document source alias maps to multiple documents")

        hit.source = candidate_source
        record = DocumentRecord(
            document_id=document_id,
            backend=hit.backend,
            public_source=candidate_source,
            fetch_url=hit.url,
            docid=hit.docid,
            admission=admission,
            hit=hit.model_copy(deep=True),
        )
        self.documents_by_id[document_id] = record
        self.document_id_by_backend_identity[identity] = document_id
        for alias in requested_aliases:
            self._remember_alias(record, alias)
        return candidate_source

    def _remember_alias(self, record: DocumentRecord, alias: str) -> None:
        previous_id = self.document_id_by_alias.get(alias)
        if previous_id is not None and previous_id != record.document_id:
            raise ValueError("Document source alias maps to multiple documents")
        self.document_id_by_alias[alias] = record.document_id
        record.aliases.add(alias)

    def document_for_alias(self, alias: str) -> DocumentRecord | None:
        document_id = self.document_id_by_alias.get(alias)
        return self.documents_by_id.get(document_id or "")

    def mark_fetched(self, identity: str) -> None:
        document_id = self.document_id_by_backend_identity.get(identity, identity)
        record = self.documents_by_id.get(document_id)
        if record is not None:
            record.fetched = True
