from __future__ import annotations

from typing import Any

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
    ) -> list[dict[str, Any]]:
        return self._transport.call(
            "llm.extract_many",
            {
                "items": items,
                "instruction": instruction,
                "schema": schema,
                "concurrency": concurrency,
            },
        )
