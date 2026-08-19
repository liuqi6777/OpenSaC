import atexit

from ._version import __version__
from .client import LazyOpenSACClient, OpenSACClient
from .models import (
    CandidateSource,
    CapabilityFailure,
    CitationRequest,
    ContentFailure,
    ContentGrepReport,
    ContentMatch,
    ContentPassage,
    ContentPassageReport,
    ContentSnippet,
    EvidenceLocator,
    EvidenceLocatorError,
    ExtractionError,
    ExtractionResult,
    FusionBatchError,
    FusionResult,
    PassageCoordinates,
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
    "CapabilityFailure",
    "CandidateSource",
    "CitationRequest",
    "ContentFailure",
    "ContentGrepReport",
    "ContentMatch",
    "ContentPassage",
    "ContentPassageReport",
    "ContentSnippet",
    "EvidenceLocator",
    "EvidenceLocatorError",
    "ExtractionError",
    "ExtractionResult",
    "FusionBatchError",
    "FusionResult",
    "OpenSACClient",
    "PassageCoordinates",
    "RetrievalMetadata",
    "RpcError",
    "SearchBatch",
    "SearchCandidate",
    "SearchHit",
    "SearchRequestInfo",
    "sdk",
    "__version__",
]
