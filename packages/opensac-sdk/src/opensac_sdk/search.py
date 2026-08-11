from __future__ import annotations

import math

from .models import (
    CandidateSource,
    FusionBatchError,
    FusionResult,
    SearchBatch,
    SearchCandidate,
    SearchHit,
    SearchRequestInfo,
)
from .transport import UnixSocketTransport


class SearchResource:
    """Retrieval, the only way a document enters this session's reach.

    One search, not one per backend. Which corpus a session searches is a
    deployment fact rather than a choice a program makes -- a session reaches
    exactly one -- so putting the backend in the method name would only make a
    program unportable between arms. Where backends genuinely differ they
    differ in a parameter: `domains` is refused by a backend that has no domain
    filter, and a depth past what a backend serves is refused rather than
    clipped. `hit.backend` still says where a document came from.

    `offset` is depth into the ranking, and it matters more than paging usually
    does: a document becomes fetchable only by being returned from a search, so
    `limit` is at once how much you can see and how much you are allowed to
    read. Without `offset`, a program certain the answer sits at rank 15 has no
    way to get there. `rank` is always the rank in the full result list, never
    the position within the returned window.
    """

    def __init__(self, transport: UnixSocketTransport) -> None:
        self._transport = transport

    def __call__(
        self,
        query: str,
        *,
        limit: int = 10,
        offset: int = 0,
        domains: list[str] | None = None,
    ) -> list[SearchHit]:
        result = self._transport.call(
            "search.query",
            {"query": query, "limit": limit, "offset": offset, "domains": domains},
        )
        return [SearchHit.model_validate(hit) for hit in result]

    def many(
        self,
        queries: list[str],
        *,
        limit_per_query: int = 10,
        offset: int = 0,
        concurrency: int = 5,
        domains: list[str] | None = None,
    ) -> list[SearchBatch]:
        result = self._transport.call(
            "search.query_many",
            {
                "queries": queries,
                "limit_per_query": limit_per_query,
                "offset": offset,
                "concurrency": concurrency,
                "domains": domains,
            },
        )
        request = SearchRequestInfo(
            limit=limit_per_query,
            offset=offset,
            domains=domains,
        )
        batches = [SearchBatch.model_validate(batch) for batch in result]
        return [
            batch
            if batch.request is not None
            else batch.model_copy(update={"request": request.model_copy(deep=True)})
            for batch in batches
        ]

    def fuse_rrf(
        self,
        batches: list[SearchBatch | dict],
        *,
        weights: list[float] | None = None,
        k: int = 60,
        limit: int | None = None,
    ) -> FusionResult:
        """Fuse search batches locally with deterministic reciprocal-rank fusion.

        This method deliberately does not use the transport. References stay
        unchanged, so every returned candidate remains valid for content and
        citation calls in the current broker session.
        """
        parsed_batches = [SearchBatch.model_validate(batch) for batch in batches]
        normalized_weights = self._validate_fusion_options(
            len(parsed_batches), weights=weights, k=k, limit=limit
        )

        input_count = 0
        batch_errors: list[FusionBatchError] = []
        candidates: dict[str, dict] = {}

        for batch_index, (batch, weight) in enumerate(
            zip(parsed_batches, normalized_weights, strict=True)
        ):
            if batch.error is not None:
                batch_errors.append(
                    FusionBatchError(
                        batch_index=batch_index,
                        query=batch.query,
                        error=batch.error,
                    )
                )
                continue

            input_count += len(batch.hits)
            best_in_batch: dict[str, tuple[int, SearchHit]] = {}
            for hit_index, hit in enumerate(batch.hits):
                if hit.rank < 1:
                    raise ValueError("Every fused search hit must have rank >= 1")
                previous = best_in_batch.get(hit.ref)
                if previous is None or (hit.rank, hit_index) < (
                    previous[1].rank,
                    previous[0],
                ):
                    best_in_batch[hit.ref] = (hit_index, hit)

            for hit_index, hit in best_in_batch.values():
                source = CandidateSource(
                    batch_index=batch_index,
                    query=batch.query,
                    backend=hit.backend,
                    rank=hit.rank,
                    score=hit.score,
                    retrieval=hit.retrieval,
                    request=batch.request,
                )
                representative_key = (hit.rank, batch_index, hit_index)
                candidate = candidates.get(hit.ref)
                if candidate is None:
                    candidates[hit.ref] = {
                        "hit": hit,
                        "representative_key": representative_key,
                        "best_rank": hit.rank,
                        "earliest_batch": batch_index,
                        "sources": [source],
                        "fused_score": weight / (k + hit.rank),
                    }
                    continue

                candidate["sources"].append(source)
                candidate["fused_score"] += weight / (k + hit.rank)
                candidate["best_rank"] = min(candidate["best_rank"], hit.rank)
                candidate["earliest_batch"] = min(
                    candidate["earliest_batch"], batch_index
                )
                if representative_key < candidate["representative_key"]:
                    candidate["hit"] = hit
                    candidate["representative_key"] = representative_key

        ordered = sorted(
            candidates.items(),
            key=lambda item: (
                -item[1]["fused_score"],
                item[1]["best_rank"],
                item[1]["earliest_batch"],
                item[0],
            ),
        )
        fused_candidates = [
            SearchCandidate(
                **candidate["hit"].model_dump(),
                sources=candidate["sources"],
                fused_score=candidate["fused_score"],
                fused_rank=fused_rank,
            )
            for fused_rank, (_, candidate) in enumerate(ordered, start=1)
        ]
        if limit is not None:
            fused_candidates = fused_candidates[:limit]

        unique_count = len(candidates)
        return FusionResult(
            candidates=fused_candidates,
            input_count=input_count,
            unique_count=unique_count,
            duplicate_count=input_count - unique_count,
            batch_errors=batch_errors,
        )

    @staticmethod
    def _validate_fusion_options(
        batch_count: int,
        *,
        weights: list[float] | None,
        k: int,
        limit: int | None,
    ) -> list[float]:
        if isinstance(k, bool) or not isinstance(k, int) or k < 0:
            raise ValueError("k must be a non-negative integer")
        if limit is not None and (
            isinstance(limit, bool) or not isinstance(limit, int) or limit < 0
        ):
            raise ValueError("limit must be a non-negative integer or None")

        if weights is None:
            normalized = [1.0] * batch_count
        else:
            if len(weights) != batch_count:
                raise ValueError("weights must align one-to-one with batches")
            normalized = []
            for weight in weights:
                if isinstance(weight, bool) or not isinstance(weight, (int, float)):
                    raise ValueError("weights must contain only finite numbers")
                normalized_weight = float(weight)
                if not math.isfinite(normalized_weight):
                    raise ValueError("weights must contain only finite numbers")
                if normalized_weight < 0:
                    raise ValueError("weights must be non-negative")
                normalized.append(normalized_weight)

        if normalized and not any(weight > 0 for weight in normalized):
            raise ValueError("at least one weight must be greater than zero")
        return normalized
