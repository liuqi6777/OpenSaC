"""Async research capabilities executed in the current Python process."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Annotated, Any

from pydantic import Field, StringConstraints, TypeAdapter, ValidationError, validate_call

from .compose import fuse
from .config import Settings
from .contracts import (
    BatchItem,
    Capabilities,
    Completion,
    Document,
    Extraction,
    RerankResult,
    SearchHit,
    URLString,
)
from .errors import (
    CapabilityError,
    ErrorInfo,
    InvalidRequestError,
    ProviderResponseError,
    RequestTimeoutError,
    RuntimeClosedError,
)
from .provider import (
    FetchProvider,
    LLMProvider,
    ProviderContext,
    Providers,
    RerankProvider,
    SearchProvider,
)
from .structured import parse_extraction, schema_validator

Query = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
QueryVariants = Annotated[list[Query], Field(min_length=1, max_length=10)]
SearchQuery = Query | QueryVariants
Text = Annotated[str, StringConstraints(min_length=1, max_length=200_000)]
SearchLimit = Annotated[int, Field(ge=1, le=100, strict=True)]
TopN = Annotated[int, Field(ge=1, le=100, strict=True)]
Prompt = Annotated[str, StringConstraints(min_length=1, max_length=204_096)]
QueryBatch = Annotated[list[Query], Field(min_length=1, max_length=32)]
URLBatch = Annotated[list[URLString], Field(min_length=1, max_length=32)]
TextBatch = Annotated[list[Text], Field(min_length=1, max_length=32)]
RerankTexts = Annotated[list[Text], Field(min_length=1, max_length=100)]

_SEARCH_RESULTS = TypeAdapter(list[SearchHit])
_DOCUMENT = TypeAdapter(Document)
_RERANK_RESULTS = TypeAdapter(list[RerankResult])
_COMPLETION = TypeAdapter(Completion)
_DEFAULT_INSTRUCTION = "Extract the requested information from the supplied text."


async def _gather[T](awaitables: list[Awaitable[T]]) -> list[T]:
    """Gather work without leaving sibling tasks running after a failure."""
    tasks: list[asyncio.Future[T]] = [asyncio.ensure_future(item) for item in awaitables]
    try:
        return list(await asyncio.gather(*tasks))
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


def _validated[**P, T](method: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[T]]:
    @wraps(method)
    def invoke(*args: P.args, **kwargs: P.kwargs) -> Awaitable[T]:
        return method(*args, **kwargs)

    checked = validate_call(invoke)

    @wraps(method)
    async def call(*args: P.args, **kwargs: P.kwargs) -> T:
        try:
            pending = checked(*args, **kwargs)
        except ValidationError as exc:
            raise InvalidRequestError("Invalid request parameters.") from exc
        # Catch argument validation only, not errors raised during provider execution.
        return await pending

    return call


class Runtime:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        search_provider: SearchProvider | None = None,
        fetch_provider: FetchProvider | None = None,
        rerank_provider: RerankProvider | None = None,
        llm_provider: LLMProvider | None = None,
    ) -> None:
        self.settings = settings if settings is not None else Settings()
        self.context = ProviderContext(
            request_timeout=self.settings.request_timeout,
            max_response_bytes=self.settings.max_response_bytes,
        )
        self.providers = Providers(
            self.settings,
            self.context,
            search=search_provider,
            fetch=fetch_provider,
            rerank=rerank_provider,
            llm=llm_provider,
        )
        self._slots = asyncio.Semaphore(self.settings.max_concurrency)
        self._closed = False

    def _check_open(self) -> None:
        if self._closed:
            raise RuntimeClosedError("The capability runtime is closed.")

    async def _run[T](self, operation: Callable[[], Awaitable[T]]) -> T:
        self._check_open()
        async with self._slots:
            try:
                async with asyncio.timeout(self.settings.request_timeout):
                    return await operation()
            except TimeoutError as exc:
                raise RequestTimeoutError("Request deadline exceeded.") from exc

    @staticmethod
    def _result[T](adapter: TypeAdapter[T], result: Any) -> T:
        try:
            return adapter.validate_python(result)
        except ValidationError as exc:
            raise ProviderResponseError("Capability provider returned invalid data.") from exc

    async def _search(self, query: str, limit: int) -> list[SearchHit]:
        async def run() -> list[SearchHit]:
            result = await self.providers.search.get().search(query, limit)
            return self._result(_SEARCH_RESULTS, result)[:limit]

        return await self._run(run)

    @_validated
    async def search(self, query: SearchQuery, *, limit: SearchLimit = 5) -> list[SearchHit]:
        if isinstance(query, str):
            return await self._search(query, limit)

        unique_queries = list(dict.fromkeys(query))

        result_sets = await _gather([self._search(variant, limit) for variant in unique_queries])
        return fuse(result_sets)[:limit]

    @_validated
    async def fetch(self, url: URLString) -> Document:
        async def run() -> Document:
            result = await self.providers.fetch.get().fetch(url)
            document = self._result(_DOCUMENT, result)
            if document.url != url:
                raise ProviderResponseError("Fetch provider changed the requested URL.")
            return document

        return await self._run(run)

    @_validated
    async def rerank(
        self,
        query: Query,
        documents: RerankTexts,
        *,
        top_n: TopN | None = None,
    ) -> list[RerankResult]:
        if top_n is not None and top_n > len(documents):
            raise InvalidRequestError("top_n exceeds document count.")
        if sum(map(len, documents)) > 500_000:
            raise InvalidRequestError("Rerank input exceeds limit.")

        async def run() -> list[RerankResult]:
            result = await self.providers.rerank.get().rerank(query, documents, top_n=top_n)
            ranked = self._result(_RERANK_RESULTS, result)
            expected = top_n or len(documents)
            indices = [item.index for item in ranked]
            if (
                len(ranked) != expected
                or len(set(indices)) != expected
                or any(index >= len(documents) for index in indices)
            ):
                raise ProviderResponseError("Rerank provider returned invalid positions.")
            return sorted(ranked, key=lambda item: item.relevance_score, reverse=True)

        return await self._run(run)

    async def _generate(
        self,
        prompt: str,
        *,
        schema: dict[str, Any] | None = None,
    ) -> Completion:
        return self._result(
            _COMPLETION, await self.providers.llm.get().complete(prompt, schema=schema)
        )

    @_validated
    async def complete(self, prompt: Prompt) -> Completion:
        return await self._run(lambda: self._generate(prompt))

    async def _extract(
        self,
        text: str,
        schema: dict[str, Any],
        *,
        instruction: str,
    ) -> Extraction:
        async def run() -> Extraction:
            completion = await self._generate(
                f"{instruction}\nReturn JSON matching the supplied schema.\n\nSource text:\n{text}",
                schema=schema,
            )
            data = parse_extraction(
                completion.text, schema, max_bytes=self.settings.max_response_bytes
            )
            return Extraction(data=data, model=completion.model, usage=completion.usage)

        return await self._run(run)

    @_validated
    async def extract(
        self,
        text: Text,
        schema: dict[str, Any],
        *,
        instruction: Query = _DEFAULT_INSTRUCTION,
    ) -> Extraction:
        self._check_open()
        schema_validator(schema)
        return await self._extract(text, schema, instruction=instruction)

    async def _batch[Q, T](
        self,
        items: list[Q],
        call: Callable[[Q], Awaitable[T]],
    ) -> list[BatchItem[T]]:
        self._check_open()

        async def one(item: Q) -> BatchItem[T]:
            try:
                return BatchItem(data=await call(item))
            except CapabilityError as exc:
                return BatchItem(
                    error=ErrorInfo(code=exc.code, message=exc.message, retryable=exc.retryable)
                )

        return await _gather([one(item) for item in items])

    @_validated
    async def search_many(
        self,
        queries: QueryBatch,
        *,
        limit: SearchLimit = 5,
    ) -> list[BatchItem[list[SearchHit]]]:
        return await self._batch(queries, lambda query: self.search(query, limit=limit))

    @_validated
    async def fetch_many(self, urls: URLBatch) -> list[BatchItem[Document]]:
        return await self._batch(urls, self.fetch)

    @_validated
    async def complete_many(self, prompts: TextBatch) -> list[BatchItem[Completion]]:
        return await self._batch(prompts, self.complete)

    @_validated
    async def extract_many(
        self,
        texts: TextBatch,
        schema: dict[str, Any],
        *,
        instruction: Query = _DEFAULT_INSTRUCTION,
    ) -> list[BatchItem[Extraction]]:
        self._check_open()
        schema_validator(schema)
        return await self._batch(
            texts, lambda text: self._extract(text, schema, instruction=instruction)
        )

    def capabilities(self) -> Capabilities:
        self._check_open()
        return Capabilities(
            methods=[
                "search",
                "search.many",
                "content.fetch",
                "content.fetch_many",
                "rerank",
                "llm.complete",
                "llm.complete_many",
                "llm.extract",
                "llm.extract_many",
            ],
            limits={
                "batch_size": 32,
                "search_reformulations": 10,
                "search_limit": 100,
                "rerank_documents": 100,
                "rerank_input_chars": 500_000,
                "llm_prompt_chars": 204_096,
                "llm_text_chars": 200_000,
                "response_bytes": self.settings.max_response_bytes,
            },
        )

    async def aclose(self) -> None:
        if not self._closed:
            self._closed = True
            await self.providers.aclose()
