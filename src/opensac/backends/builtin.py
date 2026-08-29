from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from opensac import _optional
from opensac.backends.catalog import BackendBuildContext, backend_provider
from opensac.backends.document import DocumentBackend
from opensac.backends.document.jina import JinaReaderBackend
from opensac.backends.document.local_http import LocalDocumentBackend
from opensac.backends.llm import LLMBackend, OpenAICompatibleBackend
from opensac.backends.rerank import JinaReranker, LexicalReranker, TextReranker
from opensac.backends.search import SearchBackend
from opensac.backends.search.local_http import LocalSearchBackend
from opensac.backends.search.serper import SerperBackend
from opensac.config import DEFAULT_LOCAL_BACKEND_BASE_URL


@backend_provider(role="search", name="local")
def local_search(context: BackendBuildContext) -> SearchBackend:
    config = context.settings.backends.search
    _reject_options("search", "local", config.options)
    return LocalSearchBackend(
        config.base_url or DEFAULT_LOCAL_BACKEND_BASE_URL,
        timeout=context.timeout("search"),
    )


@backend_provider(role="search", name="serper")
def serper_search(context: BackendBuildContext) -> SearchBackend:
    config = context.settings.backends.search
    _reject_options("search", "serper", config.options)
    return SerperBackend(
        context.settings.serper_api_key,
        timeout=context.timeout("search"),
        base_url=config.base_url,
    )


@backend_provider(role="document", name="local")
def local_document(context: BackendBuildContext) -> DocumentBackend:
    config = context.settings.backends.document
    _reject_options("document", "local", config.options)
    return LocalDocumentBackend(
        config.base_url or DEFAULT_LOCAL_BACKEND_BASE_URL,
        timeout=context.timeout("document"),
    )


@backend_provider(role="document", name="jina")
def jina_document(context: BackendBuildContext) -> DocumentBackend:
    config = context.settings.backends.document
    _reject_options("document", "jina", config.options)
    return JinaReaderBackend(
        context.settings.jina_api_key,
        timeout=context.timeout("document"),
        base_url=config.base_url,
    )


@backend_provider(role="rerank", name="lexical")
def lexical_reranker(context: BackendBuildContext) -> TextReranker:
    _reject_options("rerank", "lexical", context.settings.backends.rerank.options)
    return LexicalReranker()


@backend_provider(role="rerank", name="jina")
def jina_reranker(context: BackendBuildContext) -> TextReranker:
    config = context.settings.backends.rerank
    _reject_options("rerank", "jina", config.options)
    return JinaReranker(
        api_key=context.settings.jina_api_key,
        model=config.model,
        timeout=context.timeout("rerank"),
    )


@backend_provider(role="llm", name="openai_compatible")
def openai_compatible_llm(context: BackendBuildContext) -> LLMBackend:
    _optional.require_extra("Pipeline LLM support", "llm", ("jsonschema", "openai"))
    config = context.settings.backends.llm
    _reject_options("llm", "openai_compatible", config.options)
    return OpenAICompatibleBackend(
        model=config.model,
        api_key=context.settings.model_api_key,
        base_url=config.base_url,
    )


def _reject_options(role: str, provider: str, options: Mapping[str, Any]) -> None:
    if options:
        raise ValueError(
            f"Built-in {role} backend provider {provider!r} does not accept options: "
            f"{sorted(options)}"
        )
