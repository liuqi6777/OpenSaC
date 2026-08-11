import atexit

from .client import LazyOpenSACClient, OpenSACClient
from .models import (
    CandidateSource,
    CitationRequest,
    ContentMatch,
    ContentSnippet,
    EvidenceLocator,
    ExtractionError,
    ExtractionResult,
    FusionBatchError,
    FusionResult,
    RetrievalMetadata,
    RpcError,
    SearchBatch,
    SearchCandidate,
    SearchHit,
    SearchRequestInfo,
)
from .transport import BrokerError

sdk = LazyOpenSACClient()
atexit.register(sdk.close)

__all__ = [
    "BrokerError",
    "CandidateSource",
    "CitationRequest",
    "ContentMatch",
    "ContentSnippet",
    "EvidenceLocator",
    "ExtractionError",
    "ExtractionResult",
    "FusionBatchError",
    "FusionResult",
    "OpenSACClient",
    "RetrievalMetadata",
    "RpcError",
    "SearchBatch",
    "SearchCandidate",
    "SearchHit",
    "SearchRequestInfo",
    "sdk",
]
