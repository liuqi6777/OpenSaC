"""Explicit client plus a lazy singleton for ordinary research programs."""

from __future__ import annotations

import threading
from collections.abc import Awaitable, Callable, Sequence
from types import TracebackType
from typing import Any, Self

from opensac.config import Settings

from .bridge import SyncRuntime
from .contracts import (
    BatchItem,
    Capabilities,
    Completion,
    Document,
    Extraction,
    SearchHit,
)
from .errors import ClientClosedError, InvalidRequestError
from .runtime import Runtime


class Search:
    def __init__(self, client: Client) -> None:
        self._client = client

    def __call__(self, query: str | list[str], *, limit: int = 5) -> list[SearchHit]:
        return self._client._call(lambda runtime: runtime.search(query, limit=limit))

    def many(self, queries: list[str], *, limit: int = 5) -> list[BatchItem[list[SearchHit]]]:
        return self._client._call(lambda runtime: runtime.search_many(queries, limit=limit))


class Content:
    def __init__(self, client: Client) -> None:
        self._client = client

    def fetch(self, url: str) -> Document:
        return self._client._call(lambda runtime: runtime.fetch(url))

    def fetch_many(self, urls: list[str]) -> list[BatchItem[Document]]:
        return self._client._call(lambda runtime: runtime.fetch_many(urls))


class Rerank:
    def __init__(self, client: Client) -> None:
        self._client = client

    def __call__[T](
        self,
        query: str,
        items: Sequence[T],
        *,
        text: Callable[[T], str] | None = None,
        top_n: int | None = None,
    ) -> list[T]:
        """Rerank original objects; only their selected text crosses the provider boundary.

        SearchHit uses title + snippet, Document uses text, and strings pass through.
        Other objects require a text callback. Empty input returns [] without a request.
        """
        self._client._check_open()
        if top_n is not None and (type(top_n) is not int or top_n < 1):
            raise InvalidRequestError("top_n must be a positive integer.")
        values = list(items)
        if not values:
            return []

        def default_text(item: T) -> str:
            if isinstance(item, SearchHit):
                return f"{item.title}\n{item.snippet}".strip()
            if isinstance(item, Document):
                return item.text
            if isinstance(item, str):
                return item
            raise TypeError("Provide a text callback for this item type.")

        select = text if text is not None else default_text
        documents = [select(item) for item in values]
        ranked = self._client._call(
            lambda runtime: runtime.rerank(query, documents, top_n=top_n),
        )
        return [values[result.index] for result in ranked]


class LLM:
    def __init__(self, client: Client) -> None:
        self._client = client

    def complete(self, prompt: str) -> Completion:
        return self._client._call(lambda runtime: runtime.complete(prompt))

    def complete_many(self, prompts: list[str]) -> list[BatchItem[Completion]]:
        return self._client._call(lambda runtime: runtime.complete_many(prompts))

    def extract(
        self,
        text: str,
        schema: dict[str, Any],
        *,
        instruction: str = "Extract the requested information from the supplied text.",
    ) -> Extraction:
        return self._client._call(
            lambda runtime: runtime.extract(text, schema, instruction=instruction)
        )

    def extract_many(
        self,
        texts: list[str],
        schema: dict[str, Any],
        *,
        instruction: str = "Extract the requested information from the supplied text.",
    ) -> list[BatchItem[Extraction]]:
        return self._client._call(
            lambda runtime: runtime.extract_many(texts, schema, instruction=instruction)
        )


class Client:
    def __init__(self, *, settings: Settings | None = None) -> None:
        self._runtime = SyncRuntime(settings)
        self._closed = False
        self.search = Search(self)
        self.content = Content(self)
        self.rerank = Rerank(self)
        self.llm = LLM(self)

    def _check_open(self) -> None:
        if self._closed:
            raise ClientClosedError("The client is closed.")

    def _call[T](self, operation: Callable[[Runtime], Awaitable[T]]) -> T:
        self._check_open()
        return self._runtime.call(operation)

    def capabilities(self) -> Capabilities:
        self._check_open()
        return self._runtime.call(lambda runtime: runtime.capabilities())

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._runtime.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


class LazySDK:
    """Reads provider configuration on first use, never at import time."""

    def __init__(self) -> None:
        self._client: Client | None = None
        self._lock = threading.Lock()

    def _get(self) -> Client:
        with self._lock:
            if self._client is None:
                self._client = Client()
            return self._client

    @property
    def search(self) -> Search:
        return self._get().search

    @property
    def content(self) -> Content:
        return self._get().content

    @property
    def rerank(self) -> Rerank:
        return self._get().rerank

    @property
    def llm(self) -> LLM:
        return self._get().llm

    def capabilities(self) -> Capabilities:
        return self._get().capabilities()

    def close(self) -> None:
        """Release connections after active calls finish; the next use reads config again."""
        with self._lock:
            if self._client is not None:
                self._client.close()
                self._client = None
