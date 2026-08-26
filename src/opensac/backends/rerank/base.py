"""Provider boundary for generic text rerank backends."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


class RerankScore(BaseModel):
    """Provider score for one text candidate, addressed by request index."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    index: int = Field(ge=0)
    score: float = Field(allow_inf_nan=False)


class TextReranker(Protocol):
    """Backend adapter for scoring arbitrary text candidates against a query."""

    name: str
    provider_identity: str

    def preflight(self) -> None: ...

    async def rerank(
        self,
        query: str,
        documents: list[str],
    ) -> list[RerankScore]: ...


@runtime_checkable
class ClosableTextReranker(Protocol):
    async def aclose(self) -> None: ...
