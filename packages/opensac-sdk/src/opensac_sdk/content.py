from __future__ import annotations

from .models import ContentSnippet
from .transport import UnixSocketTransport


class ContentResource:
    def __init__(self, transport: UnixSocketTransport) -> None:
        self._transport = transport

    def get_many(self, refs: list[str]) -> list[ContentSnippet]:
        result = self._transport.call("content.get_many", {"refs": refs})
        return [ContentSnippet.model_validate(item) for item in result]

    def snippets(
        self,
        query: str,
        refs: list[str],
        *,
        max_tokens: int = 4000,
        max_tokens_per_page: int = 1000,
    ) -> list[ContentSnippet]:
        result = self._transport.call(
            "content.snippets",
            {
                "query": query,
                "refs": refs,
                "max_tokens": max_tokens,
                "max_tokens_per_page": max_tokens_per_page,
            },
        )
        return [ContentSnippet.model_validate(item) for item in result]
