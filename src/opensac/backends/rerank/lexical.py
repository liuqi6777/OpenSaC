from __future__ import annotations

import math
import re
from collections import Counter

from opensac.backends.rerank.base import RerankScore

_TERM_PATTERN = re.compile(
    r"[A-Za-z0-9]+(?:[-'][A-Za-z0-9]+)*|[\u3400-\u9fff]|[^\W\d_]+",
    flags=re.UNICODE,
)


def _terms(text: str) -> list[str]:
    return [match.group(0).lower() for match in _TERM_PATTERN.finditer(text)]


def bm25_scores(query: str, documents: list[str]) -> list[float]:
    """Return request-local BM25 scores in document order."""

    if not documents:
        return []
    query_terms = list(dict.fromkeys(_terms(query)))
    tokenized = [_terms(document) for document in documents]
    if not query_terms:
        return [0.0] * len(documents)

    document_frequency: Counter[str] = Counter()
    for terms in tokenized:
        document_frequency.update(set(terms))
    document_count = len(documents)
    average_length = sum(len(terms) for terms in tokenized) / document_count

    scores: list[float] = []
    for terms in tokenized:
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
        scores.append(score)
    return scores


class LexicalReranker:
    """Deterministic in-process BM25 text reranker."""

    name = "lexical:bm25"
    provider_name = "lexical_bm25"
    provider_identity = "lexical:bm25:v1"

    @staticmethod
    def preflight() -> None:
        return None

    async def rerank(
        self,
        query: str,
        documents: list[str],
    ) -> list[RerankScore]:
        return [
            RerankScore(index=index, score=score)
            for index, score in enumerate(bm25_scores(query, documents))
        ]
