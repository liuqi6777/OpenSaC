from __future__ import annotations

import json
from typing import Any

from .models import ExtractionResult
from .transport import UnixSocketTransport


class LLMResource:
    def __init__(self, transport: UnixSocketTransport) -> None:
        self._transport = transport

    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        temperature: float = 0.2,
        max_tokens: int | None = None,
    ) -> str:
        """Run one free-form completion as an in-pipeline subroutine.

        Use this for planning, synthesis, or coverage analysis where the answer
        does not fit a fixed schema. Prefer `extract_many` whenever the result
        should be structured; a schema is easier for downstream code to consume
        than prose.
        """
        return self._transport.call(
            "llm.complete",
            {
                "prompt": prompt,
                "system": system,
                "temperature": temperature,
                "max_tokens": max_tokens,
            },
        )

    def complete_many(
        self,
        prompts: list[str],
        *,
        system: str | None = None,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        concurrency: int = 4,
    ) -> list[str]:
        """Fan out free-form completions. Results align with `prompts` by index."""
        return self._transport.call(
            "llm.complete_many",
            {
                "prompts": prompts,
                "system": system,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "concurrency": concurrency,
            },
        )

    def extract_many(
        self,
        items: list[Any],
        *,
        instruction: str,
        schema: dict[str, Any],
        concurrency: int = 4,
        max_tokens: int | None = None,
        repair_attempts: int = 0,
    ) -> list[ExtractionResult]:
        if repair_attempts not in {0, 1}:
            raise ValueError("repair_attempts must be 0 or 1")
        if not isinstance(schema, dict):
            raise ValueError("schema must be a JSON-serializable object")
        self._ensure_json_serializable(schema, "schema")
        if not isinstance(items, list):
            raise ValueError("items must be a list")
        for index, item in enumerate(items):
            self._ensure_json_serializable(item, f"items[{index}]")

        params = {
            "items": items,
            "instruction": instruction,
            "schema": schema,
            "concurrency": concurrency,
            "repair_attempts": repair_attempts,
        }
        if max_tokens is not None:
            params["max_tokens"] = max_tokens
        result = self._transport.call("llm.extract_many", params)
        return [ExtractionResult.model_validate(item) for item in result]

    @staticmethod
    def _ensure_json_serializable(value: Any, field: str) -> None:
        try:
            json.dumps(value, allow_nan=False)
        except (TypeError, ValueError, OverflowError, RecursionError) as exc:
            raise ValueError(f"{field} must be JSON serializable: {exc}") from exc
