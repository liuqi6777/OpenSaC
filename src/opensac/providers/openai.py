"""Non-streaming text generation through a configured Chat Completions endpoint."""

from typing import Any

from ..contracts import Completion, TokenUsage
from ..errors import (
    ConfigurationError,
    GenerationIncompleteError,
    ModelRefusalError,
    NotConfiguredError,
    ProviderResponseError,
)
from .base import ProviderHTTP


class OpenAILLM(ProviderHTTP):
    async def complete(self, prompt: str, *, schema: dict[str, Any] | None = None) -> Completion:
        model = self.config.model
        if self.config.base_url is None or not model:
            raise NotConfiguredError("LLM endpoint and model are required.")
        reserved = {
            "messages",
            "model",
            "stream",
            "n",
            "response_format",
            "max_completion_tokens",
            "max_tokens",
            "temperature",
            "tools",
            "tool_choice",
            "functions",
            "function_call",
        }
        if reserved.intersection(self.config.options):
            raise ConfigurationError("LLM options override managed fields.")
        body: dict[str, Any] = {
            **self.config.options,
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_completion_tokens": self.config.max_tokens,
            "stream": False,
            "n": 1,
        }
        if self.config.temperature is not None:
            body["temperature"] = self.config.temperature
        if schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "extraction",
                    "strict": True,
                    "schema": schema,
                },
            }
        payload = await self.request_json(
            str(self.config.base_url), "chat/completions", self.config.api_key, body
        )
        try:
            choice = payload["choices"][0]
            message = choice["message"]
            if message.get("refusal"):
                raise ModelRefusalError("Model declined the request.")
            if choice["finish_reason"] == "length":
                raise GenerationIncompleteError("Model output reached its limit.")
            if choice["finish_reason"] != "stop" or not isinstance(message["content"], str):
                raise ValueError("Expected finished text generation")
            usage = payload.get("usage")
            return Completion(
                text=message["content"],
                model=payload["model"],
                usage=TokenUsage(
                    input_tokens=usage.get("prompt_tokens"),
                    output_tokens=usage.get("completion_tokens"),
                    total_tokens=usage.get("total_tokens"),
                )
                if usage is not None
                else None,
            )
        except (ValueError, KeyError, TypeError, IndexError, AttributeError) as exc:
            raise ProviderResponseError("LLM provider returned invalid data.") from exc
