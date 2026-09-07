"""Local result composition. No provider calls, hidden state or score normalization."""

import math
from collections.abc import Callable, Hashable, Iterable, Sequence
from typing import Any


def _url(item: Any) -> Hashable:
    url = getattr(item, "url", None)
    if not isinstance(url, str) or not url:
        raise ValueError("Items need a URL or an explicit key function.")
    return url


def dedup[T](items: Iterable[T], *, key: Callable[[T], Hashable] | None = None) -> list[T]:
    """Keep the first item per exact URL (or custom key), preserving input order."""
    identify = key if key is not None else _url
    seen: set[Hashable] = set()
    result = []
    for item in items:
        identity = identify(item)
        if identity not in seen:
            seen.add(identity)
            result.append(item)
    return result


def fuse[T](
    result_sets: Sequence[Iterable[T]],
    *,
    weights: Sequence[float] | None = None,
    k: float = 60,
    key: Callable[[T], Hashable] | None = None,
) -> list[T]:
    """Weighted reciprocal rank fusion, using list order as rank (best first).

    Each key contributes at most once per list. Returns original objects from their
    first occurrence in a positive-weight list; original metadata is not rewritten.
    Equal scores retain first-seen order. Zero-weight lists contribute nothing.
    """
    if isinstance(k, bool) or not math.isfinite(k) or k <= 0:
        raise ValueError("k must be finite and positive.")
    resolved = list(weights) if weights is not None else [1.0] * len(result_sets)
    if len(resolved) != len(result_sets):
        raise ValueError("One weight is required for each result list.")
    if any(isinstance(w, bool) or not math.isfinite(w) or w < 0 for w in resolved):
        raise ValueError("Weights must be finite and nonnegative.")
    if resolved and not any(resolved):
        raise ValueError("At least one weight must be positive.")
    # Common scaling preserves rankings and avoids overflow for large finite weights.
    scale = max(resolved, default=1.0)
    identify = key if key is not None else _url
    scores: dict[Hashable, float] = {}
    originals: dict[Hashable, T] = {}
    for items, weight in zip(result_sets, resolved, strict=True):
        if weight == 0:
            continue
        seen: set[Hashable] = set()
        for item in items:
            identity = identify(item)
            if identity in seen:
                continue
            seen.add(identity)
            scores[identity] = scores.get(identity, 0.0) + (weight / scale) / (k + len(seen))
            originals.setdefault(identity, item)
    return [originals[identity] for identity in sorted(scores, key=lambda key: -scores[key])]
