"""Research capabilities in the current Python environment."""

import atexit

from .client import Client, LazySDK
from .compose import dedup, fuse
from .contracts import (
    BatchItem,
    Capabilities,
    Completion,
    ConfigurationCheck,
    Document,
    Extraction,
    RerankResult,
    SearchHit,
    TokenUsage,
)
from .errors import ErrorInfo, OpenSACError

sdk = LazySDK()
atexit.register(sdk.close)

__all__ = [
    "BatchItem",
    "Completion",
    "ConfigurationCheck",
    "Extraction",
    "RerankResult",
    "TokenUsage",
    "Capabilities",
    "Client",
    "Document",
    "ErrorInfo",
    "OpenSACError",
    "SearchHit",
    "sdk",
    "dedup",
    "fuse",
]
