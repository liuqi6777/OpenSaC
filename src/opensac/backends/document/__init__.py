"""Document backend contracts, adapters, and representation selection."""

from .base import (
    ClosableDocumentBackend,
    DocumentBackend,
    DocumentContent,
    DocumentHandle,
    DocumentSourceKind,
)
from .fallbacks import document_fetch_candidates
from .jina import JinaReaderBackend
from .local_http import LocalDocumentBackend, parse_document_frontmatter

__all__ = [
    "ClosableDocumentBackend",
    "DocumentBackend",
    "DocumentContent",
    "DocumentHandle",
    "DocumentSourceKind",
    "JinaReaderBackend",
    "LocalDocumentBackend",
    "document_fetch_candidates",
    "parse_document_frontmatter",
]
