from __future__ import annotations

import argparse

import uvicorn

from .api import create_app
from .config import load_local_search_config


def main() -> None:
    config = load_local_search_config()
    parser = argparse.ArgumentParser(description="OpenSAC dense local search API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8081)
    parser.add_argument("--index-path", default=config.index_path)
    parser.add_argument("--corpus-path", default=config.corpus_path)
    parser.add_argument("--index-ids-path", default=config.index_ids_path)
    parser.add_argument("--model-name", default=config.model_name)
    parser.add_argument("--query-prefix", default=config.query_prefix)
    parser.add_argument("--max-length", type=int, default=config.max_length)
    parser.add_argument("--device", default=config.device)
    parser.add_argument(
        "--top-k",
        type=int,
        default=config.default_top_k,
        help="Default top_k when a request omits it (server-side).",
    )
    parser.add_argument(
        "--result-mode",
        choices=["full", "compact", "query_aware"],
        default=config.result_mode,
        help="Server-side shaping applied to every returned search snippet.",
    )
    parser.add_argument(
        "--snippet-max-tokens", type=int, default=config.snippet_max_tokens
    )
    parser.add_argument(
        "--compact-snippet-tokens",
        type=int,
        default=config.compact_snippet_tokens,
    )
    parser.add_argument(
        "--query-aware-snippet-tokens",
        type=int,
        default=config.query_aware_snippet_tokens,
    )
    parser.add_argument(
        "--max-concurrency",
        type=int,
        default=config.max_concurrency,
        help="Max simultaneous search requests (dense retrieval: 1–4 recommended).",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=config.batch_size,
        help="Dense query microbatch size (default: LOCAL_SEARCH_BATCH_SIZE env var, or 16).",
    )
    parser.add_argument(
        "--batch-token-budget",
        type=int,
        default=config.batch_token_budget,
        help="Dense padded-token budget per microbatch (default: 8192).",
    )
    parser.add_argument(
        "--max-queries-per-request",
        type=int,
        default=config.max_queries_per_request,
        help="Maximum queries accepted by one /search_many request (default: 64).",
    )
    parser.add_argument(
        "--max-query-chars",
        type=int,
        default=config.max_query_chars,
        help="Maximum characters accepted in one query (default: 4096).",
    )
    parser.add_argument(
        "--max-top-k",
        type=int,
        default=config.max_top_k,
        help="Maximum top_k accepted by search endpoints (default: 600).",
    )
    parser.add_argument(
        "--limit-concurrency",
        type=int,
        default=None,
        help="uvicorn-level connection cap (applies before requests reach the app). "
        "Defaults to 4× --max-concurrency.",
    )
    args = parser.parse_args()

    max_concurrency: int = max(1, args.max_concurrency)
    limit_concurrency: int | None = args.limit_concurrency or (
        max_concurrency * 4
    )

    create_app_kwargs: dict = dict(
        dense_model_name=args.model_name,
        dense_index_path=args.index_path,
        dense_corpus_path=args.corpus_path,
        index_ids_path=args.index_ids_path,
        dense_query_prefix=args.query_prefix,
        dense_max_length=args.max_length,
        dense_device=args.device,
        default_top_k=args.top_k,
        result_mode=args.result_mode,
        snippet_max_tokens=args.snippet_max_tokens,
        compact_snippet_tokens=args.compact_snippet_tokens,
        query_aware_snippet_tokens=args.query_aware_snippet_tokens,
    )
    create_app_kwargs["max_concurrency"] = max_concurrency
    create_app_kwargs["batch_size"] = max(1, args.batch_size)
    create_app_kwargs["batch_token_budget"] = max(1, args.batch_token_budget)
    create_app_kwargs["max_queries_per_request"] = max(
        1, args.max_queries_per_request
    )
    create_app_kwargs["max_query_chars"] = max(1, args.max_query_chars)
    create_app_kwargs["max_top_k"] = max(1, args.max_top_k)

    app = create_app(**create_app_kwargs)
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        limit_concurrency=limit_concurrency,
    )


if __name__ == "__main__":
    main()
