import atexit as _atexit

from ._version import __version__
from .client import LazyOpenSACClient as _LazyOpenSACClient

sdk = _LazyOpenSACClient()
_atexit.register(sdk.close)

__all__ = [
    "sdk",
    "__version__",
]
