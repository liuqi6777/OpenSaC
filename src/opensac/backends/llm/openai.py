"""OpenAI-compatible chat completion backend."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

from opensac.backends.llm.base import LLMResponse

if TYPE_CHECKING:
    from openai import AsyncOpenAI
    from openai.types.chat import ChatCompletionMessageParam


class OpenAICompatibleBackend:
    provider_name = "openai_compatible"

    def __init__(
        self,
        *,
        model: str,
        api_key: str = "",
        base_url: str | None = None,
        client: AsyncOpenAI | None = None,
    ) -> None:
        model = model.strip()
        if not model:
            raise ValueError("LLM backend model must not be empty")
        self.model = model
        self.api_key = api_key
        self.base_url = base_url
        self._client: AsyncOpenAI | None = client

    @property
    def name(self) -> str:
        return f"openai-compatible:{self.model}"

    @property
    def provider_identity(self) -> str:
        endpoint = self.base_url or "https://api.openai.com/v1"
        material = "\0".join((endpoint, self.api_key, self.model))
        digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
        return f"openai-compatible:{digest}"

    def _new_client(self) -> AsyncOpenAI:
        from openai import AsyncOpenAI

        return AsyncOpenAI(
            api_key=self.api_key or "not-configured",
            base_url=self.base_url,
            # ProviderRuntime is the single owner of retries and deadlines.
            max_retries=0,
        )

    def _openai(self) -> AsyncOpenAI:
        if self._client is None:
            self._client = self._new_client()
        return self._client

    async def aclose(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.close()

    async def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_object: bool = False,
    ) -> LLMResponse:
        client = self._openai()
        messages: list[ChatCompletionMessageParam] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        options: dict[str, Any] = {}
        if temperature is not None:
            options["temperature"] = temperature
        if max_tokens is not None:
            options["max_completion_tokens"] = max_tokens
        if json_object:
            options["response_format"] = {"type": "json_object"}
        response = await client.chat.completions.create(
            model=self.model,
            messages=messages,
            **options,
        )
        usage = getattr(response, "usage", None)
        tokens = int(getattr(usage, "total_tokens", 0) or 0)
        return LLMResponse(
            content=response.choices[0].message.content or "",
            tokens=tokens,
        )
