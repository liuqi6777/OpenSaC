"""Provider boundary for broker-facing language model backends."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


class LLMResponse(BaseModel):
    """Normalized text and usage returned by one model request."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    content: str
    tokens: int = Field(ge=0)


class LLMBackend(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def provider_identity(self) -> str: ...

    async def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_object: bool = False,
    ) -> LLMResponse: ...


@runtime_checkable
class ClosableLLMBackend(Protocol):
    async def aclose(self) -> None: ...
