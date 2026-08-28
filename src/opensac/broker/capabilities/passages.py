"""Broker-owned passage segmentation, lexical scoring, and deterministic selection."""

from __future__ import annotations

from bisect import bisect_right
from collections import Counter
from dataclasses import dataclass, replace
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from opensac.backends.document import DocumentHandle
from opensac.backends.rerank import bm25_scores


class PassageCoordinates(BaseModel):
    """Exact half-open coordinates of a passage in normalized document text."""

    model_config = ConfigDict(extra="forbid")

    start_line: int = Field(ge=1)
    start_character: int = Field(ge=0)
    end_line: int = Field(ge=1)
    end_character: int = Field(ge=0)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        start = (self.start_line, self.start_character)
        end = (self.end_line, self.end_character)
        if end <= start:
            raise ValueError("passage coordinates must describe a non-empty range")
        return self


@dataclass(frozen=True)
class PassageCandidate:
    route: str
    handle: DocumentHandle
    input_index: int
    title: str
    url: str | None
    date: str | None
    text: str
    start: int
    end: int
    coordinates: PassageCoordinates
    prefilter_score: float = 0.0


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


def score_passage_prefilter(
    query: str,
    candidates: list[PassageCandidate],
) -> list[PassageCandidate]:
    """Apply request-local BM25; scores compare only within this call."""

    if not candidates:
        return []
    scores = bm25_scores(query, [candidate.text for candidate in candidates])
    return [
        replace(candidate, prefilter_score=score)
        for candidate, score in zip(candidates, scores, strict=True)
    ]


def prefilter_passage_candidates(
    candidates: list[PassageCandidate],
    *,
    limit_per_source: int,
    limit: int,
) -> list[PassageCandidate]:
    grouped: dict[str, list[PassageCandidate]] = {}
    for candidate in candidates:
        grouped.setdefault(candidate.handle.source, []).append(candidate)

    retained: list[PassageCandidate] = []
    for rows in grouped.values():
        retained.extend(
            sorted(
                rows,
                key=lambda candidate: (
                    -candidate.prefilter_score,
                    candidate.start,
                    candidate.end,
                ),
            )[: max(8, limit_per_source)]
        )
    retained.sort(
        key=lambda candidate: (
            -candidate.prefilter_score,
            candidate.input_index,
            candidate.start,
            candidate.end,
        )
    )
    return retained[:limit]


def select_passage_candidates(
    candidates: list[tuple[PassageCandidate, float]],
    *,
    limit_per_source: int,
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
        if per_source[candidate.handle.source] >= limit_per_source:
            continue
        per_source[candidate.handle.source] += 1
        selected.append((candidate, score))
        if len(selected) >= limit:
            break
    return selected
