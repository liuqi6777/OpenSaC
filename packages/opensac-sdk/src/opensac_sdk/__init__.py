import atexit

from .client import LazyOpenSACClient, OpenSACClient
from .models import ContentMatch, ContentSnippet, SearchBatch, SearchHit

sdk = LazyOpenSACClient()
atexit.register(sdk.close)

__all__ = [
    "ContentMatch",
    "ContentSnippet",
    "OpenSACClient",
    "SearchBatch",
    "SearchHit",
    "sdk",
]
