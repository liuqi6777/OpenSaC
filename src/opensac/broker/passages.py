"""Broker-owned passage segmentation, lexical scoring, and deterministic selection."""

from __future__ import annotations

import math
import re
from bisect import bisect_right
from collections import Counter
from dataclasses import dataclass, replace

from opensac._contracts import PassageCoordinates, SearchHit

_TERM_PATTERN = re.compile(
    r"[A-Za-z0-9]+(?:[-'][A-Za-z0-9]+)*|[\u3400-\u9fff]|[^\W\d_]+",
    flags=re.UNICODE,
)


@dataclass(frozen=True)
class PassageCandidate:
    hit: SearchHit
    input_index: int
    title: str
    url: str | None
    date: str | None
    text: str
    start: int
    end: int
    coordinates: PassageCoordinates
    lexical_score: float = 0.0


def normalize_document_text(text: str) -> str:
    """Normalize newline spellings without changing visible coordinates."""

    return (text or "").replace("\r\n", "\n").replace("\r", "\n")


def _coordinates(line_starts: list[int], start: int, end: int) -> PassageCoordinates:
    start_index = max(0, bisect_right(line_starts, start) - 1)
    end_index = max(0, bisect_right(line_starts, end - 1) - 1)
    return PassageCoordinates(
        start_line=start_index + 1,
        start_character=start - line_starts[start_index],
        end_line=end_index + 1,
        end_character=end - line_starts[end_index],
    )


def segment_passages(
    text: str,
    *,
    chunk_chars: int,
    overlap_chars: int,
) -> list[tuple[str, int, int, PassageCoordinates]]:
    """Return deterministic windows, preferring paragraph then line boundaries."""

    if not text or not text.strip():
        return []
    line_starts = [0, *(index + 1 for index, char in enumerate(text) if char == "\n")]
    windows: list[tuple[str, int, int, PassageCoordinates]] = []
    cursor = 0
    length = len(text)
    while cursor < length:
        while cursor < length and text[cursor].isspace():
            cursor += 1
        if cursor >= length:
            break
        hard_end = min(length, cursor + chunk_chars)
        end = hard_end
        if hard_end < length:
            boundary_floor = cursor + max(1, chunk_chars // 2)
            paragraph = text.rfind("\n\n", boundary_floor, hard_end + 1)
            line = text.rfind("\n", boundary_floor, hard_end + 1)
            if paragraph >= boundary_floor:
                end = paragraph
            elif line >= boundary_floor:
                end = line
        while end > cursor and text[end - 1].isspace():
            end -= 1
        if end <= cursor:
            end = hard_end
        passage = text[cursor:end]
        if passage:
            windows.append((passage, cursor, end, _coordinates(line_starts, cursor, end)))
        if hard_end >= length:
            break
        cursor = max(cursor + 1, end - overlap_chars)
    return windows


def _terms(text: str) -> list[str]:
    return [match.group(0).lower() for match in _TERM_PATTERN.finditer(text)]


def score_passage_candidates(
    query: str,
    candidates: list[PassageCandidate],
) -> list[PassageCandidate]:
    """Apply request-local BM25; scores compare only within this call."""

    if not candidates:
        return []
    query_terms = list(dict.fromkeys(_terms(query)))
    tokenized = [_terms(candidate.text) for candidate in candidates]
    if not query_terms:
        return candidates
    document_frequency: Counter[str] = Counter()
    for terms in tokenized:
        document_frequency.update(set(terms))
    document_count = len(candidates)
    average_length = sum(len(terms) for terms in tokenized) / max(document_count, 1)
    scored: list[PassageCandidate] = []
    for candidate, terms in zip(candidates, tokenized, strict=True):
        counts = Counter(terms)
        document_length = len(terms)
        score = 0.0
        for term in query_terms:
            frequency = counts.get(term, 0)
            if not frequency:
                continue
            frequency_in_documents = document_frequency[term]
            inverse_document_frequency = math.log(
                1.0
                + (document_count - frequency_in_documents + 0.5) / (frequency_in_documents + 0.5)
            )
            denominator = frequency + 1.5 * (
                1.0 - 0.75 + 0.75 * document_length / max(average_length, 1.0)
            )
            score += inverse_document_frequency * frequency * 2.5 / denominator
        scored.append(replace(candidate, lexical_score=score))
    return scored


def prefilter_passage_candidates(
    candidates: list[PassageCandidate],
    *,
    max_per_source: int,
    limit: int,
) -> list[PassageCandidate]:
    grouped: dict[str, list[PassageCandidate]] = {}
    for candidate in candidates:
        grouped.setdefault(candidate.hit.source, []).append(candidate)

    retained: list[PassageCandidate] = []
    for rows in grouped.values():
        retained.extend(
            sorted(
                rows,
                key=lambda candidate: (
                    -candidate.lexical_score,
                    candidate.start,
                    candidate.end,
                ),
            )[: max(8, max_per_source)]
        )
    retained.sort(
        key=lambda candidate: (
            -candidate.lexical_score,
            candidate.input_index,
            candidate.start,
            candidate.end,
        )
    )
    return retained[:limit]


def select_passage_candidates(
    candidates: list[tuple[PassageCandidate, float]],
    *,
    max_per_source: int,
    limit: int,
) -> list[tuple[PassageCandidate, float]]:
    ordered = sorted(
        candidates,
        key=lambda item: (
            -item[1],
            item[0].input_index,
            item[0].start,
            item[0].end,
        ),
    )
    selected: list[tuple[PassageCandidate, float]] = []
    per_source: Counter[str] = Counter()
    for candidate, score in ordered:
        if per_source[candidate.hit.source] >= max_per_source:
            continue
        per_source[candidate.hit.source] += 1
        selected.append((candidate, score))
        if len(selected) >= limit:
            break
    return selected
