from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class PassageRerankResult:
    """Provider score for one candidate, addressed by its request index."""

    index: int
    score: float


class PassageReranker(Protocol):
    name: str
    provider_identity: str

    def preflight(self) -> None: ...

    async def rerank(
        self,
        query: str,
        documents: list[str],
    ) -> list[PassageRerankResult]: ...


@runtime_checkable
class ClosablePassageReranker(Protocol):
    async def aclose(self) -> None: ...
