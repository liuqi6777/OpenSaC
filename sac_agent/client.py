"""Small OpenAI-compatible client used by the ReAct loop."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from typing import Any

from openai import (
    APIConnectionError,
    APITimeoutError,
    AsyncOpenAI,
    InternalServerError,
    RateLimitError,
)

_MODEL_TIMEOUT_SECONDS = 600.0
_MODEL_MAX_RETRIES = 3
_MODEL_TEMPERATURE = 1.0
_MODEL_TOP_P = 0.95
_MODEL_PRESENCE_PENALTY = 0.0
_MODEL_MAX_TOKENS = 16_384


@dataclass(frozen=True)
class ModelConfig:
    model: str
    api_key: str = "EMPTY"
    base_url: str | None = None

    @classmethod
    def from_env(cls) -> ModelConfig:
        model = (os.getenv("AGENT_MODEL_NAME") or os.getenv("OPENAI_MODEL") or "").strip()
        if not model:
            raise ValueError("Set AGENT_MODEL_NAME (or OPENAI_MODEL).")

        return cls(
            model=model,
            api_key=(os.getenv("AGENT_API_KEY") or os.getenv("OPENAI_API_KEY") or "EMPTY"),
            base_url=(os.getenv("AGENT_API_BASE") or os.getenv("OPENAI_BASE_URL") or None),
        )


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    raw_arguments: str


@dataclass(frozen=True)
class LLMResponse:
    text: str
    reasoning: str
    tool_calls: list[ToolCall]
    metadata: dict[str, Any]


class ModelClient:
    """Native tool-calling client for OpenAI-compatible chat-completions APIs."""

    def __init__(self, config: ModelConfig | None = None) -> None:
        self.config = config or ModelConfig.from_env()
        kwargs: dict[str, Any] = {
            "api_key": self.config.api_key,
            "timeout": _MODEL_TIMEOUT_SECONDS,
        }
        if self.config.base_url:
            kwargs["base_url"] = self.config.base_url
        self._client = AsyncOpenAI(**kwargs)

    async def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> LLMResponse:
        request: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "temperature": _MODEL_TEMPERATURE,
            "top_p": _MODEL_TOP_P,
            "presence_penalty": _MODEL_PRESENCE_PENALTY,
            "max_tokens": _MODEL_MAX_TOKENS,
        }

        retryable = (
            APIConnectionError,
            APITimeoutError,
            InternalServerError,
            RateLimitError,
        )
        for attempt in range(_MODEL_MAX_RETRIES + 1):
            try:
                response = await self._client.chat.completions.create(**request)
                break
            except retryable:
                if attempt >= _MODEL_MAX_RETRIES:
                    raise
                await asyncio.sleep(min(8.0, 0.5 * (2**attempt)))

        message = response.choices[0].message
        calls: list[ToolCall] = []
        for index, raw_call in enumerate(message.tool_calls or []):
            raw_arguments = raw_call.function.arguments or "{}"
            try:
                arguments = json.loads(raw_arguments)
            except json.JSONDecodeError:
                arguments = {}
            calls.append(
                ToolCall(
                    id=raw_call.id or f"call_{index}",
                    name=raw_call.function.name,
                    arguments=arguments if isinstance(arguments, dict) else {},
                    raw_arguments=raw_arguments,
                )
            )

        reasoning = str(
            getattr(message, "reasoning", None)
            or getattr(message, "reasoning_content", None)
            or ""
        )
        usage = response.usage.model_dump() if response.usage is not None else None
        return LLMResponse(
            text=message.content or "",
            reasoning=reasoning,
            tool_calls=calls,
            metadata={
                "id": response.id,
                "model": response.model,
                "finish_reason": response.choices[0].finish_reason,
                "usage": usage,
            },
        )

    async def aclose(self) -> None:
        await self._client.close()
