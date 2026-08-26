from __future__ import annotations

import hashlib

from opensac.backends.llm.base import LLMBackend, LLMResponse
from opensac.broker.providers.execution import ProviderExecutor
from opensac.broker.session import BrokerSession
from opensac.provider import ProviderRuntime

from .base import ServiceExecution


class LLMService(ServiceExecution):
    """Reusable language-model completion service with host-owned execution policy."""

    component = "llm"

    def __init__(
        self,
        backend: LLMBackend,
        providers: ProviderExecutor,
        runtime: ProviderRuntime,
    ) -> None:
        super().__init__(backend, providers, runtime)

    @property
    def name(self) -> str:
        return self.backend.name

    async def complete(
        self,
        state: BrokerSession,
        prompt: str,
        *,
        system: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_object: bool = False,
        request_index: int = 0,
    ) -> LLMResponse:
        async def request() -> LLMResponse:
            return LLMResponse.model_validate(
                await self.backend.complete(
                    prompt,
                    system=system,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    json_object=json_object,
                )
            )

        preflight = getattr(self.backend, "preflight", None)
        return await self.run(
            state,
            request_indexes=[request_index],
            request_value={
                "model": self.backend.name,
                "prompt": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                "system": (
                    hashlib.sha256(system.encode("utf-8")).hexdigest()
                    if system is not None
                    else None
                ),
                "temperature": temperature,
                "max_tokens": max_tokens,
                "json_object": json_object,
            },
            request=request,
            preflight=preflight if callable(preflight) else None,
        )
