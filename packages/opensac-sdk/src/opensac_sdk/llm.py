from __future__ import annotations

from typing import Any

from .transport import UnixSocketTransport


class LLMResource:
    def __init__(self, transport: UnixSocketTransport) -> None:
        self._transport = transport

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
