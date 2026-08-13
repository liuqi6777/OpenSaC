from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import Any

from .searcher import DenseLocalSearcher, LocalSearcher
from .snippets import SUPPORTED_RESULT_MODES, build_snippet_payload

# ``server.py`` resolves environment variables once through LocalSearchConfig
# and passes every value explicitly. These literals only serve direct app
# construction in tests and embedded deployments.
_DEFAULT_SEARCH_CONCURRENCY = 4
_DEFAULT_SEARCH_BATCH_SIZE = 16
_DEFAULT_SEARCH_BATCH_TOKEN_BUDGET = 8192
_DEFAULT_SEARCH_MAX_QUERIES = 64
_DEFAULT_SEARCH_MAX_QUERY_CHARS = 4096
_DEFAULT_SEARCH_MAX_TOP_K = 600


class _QueryBatchTooLarge(ValueError):
    pass


def _plan_microbatches(
    queries: list[str],
    token_lengths: list[int] | None,
    *,
    batch_size: int,
    batch_token_budget: int,
) -> list[list[str]]:
    """Greedily pack ordered queries under row and padded-token ceilings."""
    if token_lengths is None:
        return [
            queries[start : start + batch_size]
            for start in range(0, len(queries), batch_size)
        ]
    if len(token_lengths) != len(queries):
        raise RuntimeError(
            "Retriever returned an invalid token-length count: "
            f"expected {len(queries)}, got {len(token_lengths)}."
        )

    planned: list[list[str]] = []
    current: list[str] = []
    current_max_tokens = 0
    for query, raw_length in zip(queries, token_lengths, strict=True):
        length = max(1, int(raw_length))
        if length > batch_token_budget:
            raise _QueryBatchTooLarge(
                f"A query requires {length} tokens after truncation, exceeding "
                f"the per-batch padded-token budget of {batch_token_budget}."
            )
        next_max_tokens = max(current_max_tokens, length)
        if current and (
            len(current) >= batch_size
            or next_max_tokens * (len(current) + 1) > batch_token_budget
        ):
            planned.append(current)
            current = []
            current_max_tokens = 0
            next_max_tokens = length
        current.append(query)
        current_max_tokens = next_max_tokens
    if current:
        planned.append(current)
    return planned


def create_app(
    *,
    searcher: LocalSearcher | None = None,
    index_path: str | None = None,
    dense_model_name: str | None = None,
    dense_index_path: str | None = None,
    dense_corpus_path: str | None = None,
    index_ids_path: str | None = None,
    dense_query_prefix: str = "",
    dense_max_length: int = 32768,
    dense_device: str = "auto",
    default_top_k: int = 5,
    result_mode: str = "full",
    snippet_max_tokens: int = 512,
    compact_snippet_tokens: int = 60,
    query_aware_snippet_tokens: int = 60,
    max_concurrency: int = _DEFAULT_SEARCH_CONCURRENCY,
    batch_size: int = _DEFAULT_SEARCH_BATCH_SIZE,
    batch_token_budget: int = _DEFAULT_SEARCH_BATCH_TOKEN_BUDGET,
    max_queries_per_request: int = _DEFAULT_SEARCH_MAX_QUERIES,
    max_query_chars: int = _DEFAULT_SEARCH_MAX_QUERY_CHARS,
    max_top_k: int = _DEFAULT_SEARCH_MAX_TOP_K,
):
    try:
        from fastapi import FastAPI, HTTPException
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "fastapi and pydantic are required to run the local search API. "
            "Install them before starting the server."
        ) from exc

    if searcher is None:
        searcher = DenseLocalSearcher(
            model_name=dense_model_name or "",
            index_path=dense_index_path or index_path or "",
            corpus_path=dense_corpus_path or "",
            index_ids_path=index_ids_path or "",
            query_prefix=dense_query_prefix,
            max_length=dense_max_length,
            device=dense_device,
        )
    batch_size = max(1, int(batch_size))
    batch_token_budget = max(1, int(batch_token_budget))
    max_queries_per_request = max(1, int(max_queries_per_request))
    max_query_chars = max(1, int(max_query_chars))
    max_top_k = max(1, int(max_top_k))
    default_top_k = max(1, int(default_top_k))
    result_mode = (result_mode or "full").strip().lower()
    if result_mode not in SUPPORTED_RESULT_MODES:
        raise ValueError(f"Unsupported local search result mode: {result_mode}")
    snippet_max_tokens = max(1, int(snippet_max_tokens))
    compact_snippet_tokens = max(1, int(compact_snippet_tokens))
    query_aware_snippet_tokens = max(1, int(query_aware_snippet_tokens))
    if default_top_k > max_top_k:
        raise ValueError(
            f"default_top_k={default_top_k} exceeds max_top_k={max_top_k}."
        )

    app = FastAPI(title="browsecomp-local-search", version="0.1.0")

    # Semaphore：限制同时进入检索逻辑的请求数，防止 GPU 显存 / 内存被同时打爆
    _semaphore = asyncio.Semaphore(max_concurrency)

    def _top_k(request: dict[str, Any]) -> int:
        try:
            top_k = max(1, int(request.get("top_k", default_top_k)))
        except (TypeError, ValueError):
            top_k = default_top_k
        if top_k > max_top_k:
            raise HTTPException(
                status_code=422,
                detail=f"'top_k' must be at most {max_top_k}.",
            )
        return top_k

    def _validate_query(query: str) -> None:
        if len(query) > max_query_chars:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"Query length {len(query)} exceeds the maximum of "
                    f"{max_query_chars} characters."
                ),
            )

    def _serialize_hits(query: str, hits_raw) -> list[dict[str, Any]]:
        hits: list[dict[str, Any]] = []
        for rank, item in enumerate(hits_raw, start=1):
            payload = asdict(item)
            payload["rank"] = rank
            raw_text = str(payload.get("snippet", ""))
            payload.update(
                build_snippet_payload(
                    query=query,
                    text=raw_text,
                    mode=result_mode,
                    snippet_max_tokens=snippet_max_tokens,
                    compact_snippet_tokens=compact_snippet_tokens,
                    query_aware_snippet_tokens=query_aware_snippet_tokens,
                )
            )
            hits.append(payload)
        return hits

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        index_metadata = getattr(searcher, "index_metadata", {})
        return {
            "status": "ok",
            "backend": searcher.backend_name,
            "default_top_k": default_top_k,
            "index_path": dense_index_path or index_path or "",
            "model_name": dense_model_name or "",
            "index_schema": index_metadata.get("schema", ""),
            "index_schema_version": index_metadata.get("version"),
            "pooling": index_metadata.get("pooling", ""),
            "max_concurrency": max_concurrency,
            "batch_size": batch_size,
            "batch_token_budget": batch_token_budget,
            "max_queries_per_request": max_queries_per_request,
            "max_query_chars": max_query_chars,
            "max_top_k": max_top_k,
            "result_mode": result_mode,
            "snippet_max_tokens": snippet_max_tokens,
            "compact_snippet_tokens": compact_snippet_tokens
            if result_mode == "compact"
            else None,
            "query_aware_snippet_tokens": query_aware_snippet_tokens
            if result_mode == "query_aware"
            else None,
        }

    @app.post("/search")
    async def search(request: dict[str, Any]) -> dict[str, Any]:
        query = request.get("query")
        if not isinstance(query, str):
            raise HTTPException(status_code=400, detail="'query' must be a string.")
        _validate_query(query)
        top_k = _top_k(request)
        async with _semaphore:
            hits_raw = await asyncio.get_running_loop().run_in_executor(
                None, searcher.search, query, top_k
            )
        return {
            "backend": searcher.backend_name,
            "result_mode": result_mode,
            "results": [{"query": query, "hits": _serialize_hits(query, hits_raw)}],
        }

    @app.post("/search_many")
    async def search_many(request: dict[str, Any]) -> dict[str, Any]:
        queries = request.get("queries")
        if not isinstance(queries, list) or not all(
            isinstance(query, str) for query in queries
        ):
            raise HTTPException(
                status_code=400,
                detail="'queries' must be a list of strings.",
            )
        if len(queries) > max_queries_per_request:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"Request contains {len(queries)} queries; at most "
                    f"{max_queries_per_request} are allowed."
                ),
            )
        for query in queries:
            _validate_query(query)
        top_k = _top_k(request)

        def run_microbatches():
            batches = []
            length_fn = getattr(searcher, "query_token_lengths", None)
            token_lengths = length_fn(queries) if callable(length_fn) else None
            planned = _plan_microbatches(
                queries,
                token_lengths,
                batch_size=batch_size,
                batch_token_budget=batch_token_budget,
            )
            for microbatch in planned:
                batches.extend(searcher.search_many(microbatch, top_k))
            return batches

        try:
            async with _semaphore:
                batches = await asyncio.get_running_loop().run_in_executor(
                    None, run_microbatches
                )
        except _QueryBatchTooLarge as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc
        if len(batches) != len(queries):
            raise HTTPException(
                status_code=500,
                detail=(
                    "Retriever returned an invalid batch size: "
                    f"expected {len(queries)}, got {len(batches)}."
                ),
            )
        return {
            "backend": searcher.backend_name,
            "result_mode": result_mode,
            "results": [
                {"query": query, "hits": _serialize_hits(query, hits_raw)}
                for query, hits_raw in zip(queries, batches, strict=True)
            ],
        }

    @app.post("/get_document")
    async def get_document(request: dict[str, Any]) -> dict[str, Any]:
        docid = str(request.get("docid", "")).strip()
        if not docid:
            raise HTTPException(status_code=400, detail="'docid' is required.")
        document = await asyncio.get_running_loop().run_in_executor(
            None, searcher.get_document, docid
        )
        if document is None:
            raise HTTPException(
                status_code=404, detail=f"Document '{docid}' not found."
            )
        return document

    return app
