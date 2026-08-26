"""Search backend contracts and adapters."""

from .base import (
    BatchSearchBackend,
    ClosableSearchBackend,
    RetrievalMetadata,
    SearchBackend,
    SearchBatch,
    SearchBatchFailure,
    SearchBatchOutcome,
    SearchHit,
)

__all__ = [
    "BatchSearchBackend",
    "ClosableSearchBackend",
    "RetrievalMetadata",
    "SearchBackend",
    "SearchBatch",
    "SearchBatchFailure",
    "SearchBatchOutcome",
    "SearchHit",
]
