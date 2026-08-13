"""Configuration for the standalone dense local-search service."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

_PROJECT_DIR = Path(__file__).resolve().parents[2]


def _env(name: str, default: str = "") -> str:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return value.strip()


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    raw = _env(name)
    if not raw:
        return max(minimum, default)
    try:
        return max(minimum, int(raw))
    except ValueError as exc:
        raise ValueError(f"{name}={raw!r} must be an integer.") from exc


@dataclass(frozen=True)
class LocalSearchConfig:
    index_path: str
    corpus_path: str
    index_ids_path: str
    model_name: str
    query_prefix: str
    max_length: int
    device: str
    default_top_k: int
    max_top_k: int
    max_concurrency: int
    batch_size: int
    batch_token_budget: int
    max_queries_per_request: int
    max_query_chars: int
    result_mode: str
    snippet_max_tokens: int
    compact_snippet_tokens: int
    query_aware_snippet_tokens: int


def load_local_search_config() -> LocalSearchConfig:
    max_top_k = _env_int("LOCAL_SEARCH_MAX_TOP_K", 600)
    default_top_k = _env_int("LOCAL_SEARCH_DEFAULT_TOP_K", 5)
    if default_top_k > max_top_k:
        raise ValueError(
            "LOCAL_SEARCH_DEFAULT_TOP_K exceeds LOCAL_SEARCH_MAX_TOP_K."
        )
    result_mode = _env("LOCAL_SEARCH_RESULT_MODE", "full").lower()
    if result_mode not in {"full", "compact", "query_aware"}:
        raise ValueError(
            "LOCAL_SEARCH_RESULT_MODE must be full, compact, or query_aware."
        )
    return LocalSearchConfig(
        index_path=_env(
            "LOCAL_SEARCH_INDEX_PATH",
            str(_PROJECT_DIR / "indexes/browsecomp-plus/index.faiss"),
        ),
        corpus_path=_env(
            "LOCAL_SEARCH_CORPUS_PATH",
            str(_PROJECT_DIR / "indexes/browsecomp-plus/corpus.jsonl"),
        ),
        index_ids_path=_env(
            "LOCAL_SEARCH_INDEX_IDS_PATH",
            str(_PROJECT_DIR / "indexes/browsecomp-plus/index_ids.json"),
        ),
        model_name=_env(
            "LOCAL_SEARCH_MODEL_NAME", "Qwen/Qwen3-Embedding-8B"
        ),
        query_prefix=_env(
            "LOCAL_SEARCH_QUERY_PREFIX",
            "Instruct: Given a web search query, retrieve relevant passages that "
            "answer the query\nQuery:",
        ),
        max_length=_env_int("LOCAL_SEARCH_MAX_LENGTH", 32768),
        device=_env("LOCAL_SEARCH_DEVICE", "auto").lower(),
        default_top_k=default_top_k,
        max_top_k=max_top_k,
        max_concurrency=_env_int("LOCAL_SEARCH_MAX_CONCURRENCY", 4),
        batch_size=_env_int("LOCAL_SEARCH_BATCH_SIZE", 16),
        batch_token_budget=_env_int(
            "LOCAL_SEARCH_BATCH_TOKEN_BUDGET", 8192
        ),
        max_queries_per_request=_env_int(
            "LOCAL_SEARCH_MAX_QUERIES_PER_REQUEST", 64
        ),
        max_query_chars=_env_int("LOCAL_SEARCH_MAX_QUERY_CHARS", 4096),
        result_mode=result_mode,
        snippet_max_tokens=_env_int(
            "LOCAL_SEARCH_SNIPPET_MAX_TOKENS", 512
        ),
        compact_snippet_tokens=_env_int(
            "LOCAL_SEARCH_COMPACT_SNIPPET_TOKENS", 60
        ),
        query_aware_snippet_tokens=_env_int(
            "LOCAL_SEARCH_QUERY_AWARE_SNIPPET_TOKENS", 60
        ),
    )
