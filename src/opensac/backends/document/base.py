"""Provider boundary for broker-facing document backends."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

DocumentSourceKind = Literal["opaque", "public_url"]


class DocumentHandle(BaseModel):
    """Provider-facing locator for one broker-authorized document."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    source: str
    url: str | None = None
    docid: str | None = None
    title: str = ""
    date: str | None = None
    metadata: Mapping[str, Any] = Field(default_factory=dict)
    representation: str = "original"


class DocumentContent(BaseModel):
    """One successfully fetched and normalized document."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    source: str
    text: str
    url: str | None = None
    title: str = ""
    date: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class DocumentBackend(Protocol):
    """Adapter for the document role and its source-admission behavior."""

    name: str
    source_kind: DocumentSourceKind
    result_cacheable: bool
    provider_identity: str

    def fetch_candidates(self, handle: DocumentHandle) -> list[DocumentHandle]:
        """Return provider representations in the order they should be attempted."""
        ...

    async def fetch(
        self,
        handle: DocumentHandle,
        *,
        query: str | None = None,
    ) -> DocumentContent:
        """Fetch and normalize one document with exactly one transport call."""
        ...


@runtime_checkable
class ClosableDocumentBackend(Protocol):
    async def aclose(self) -> None: ...
