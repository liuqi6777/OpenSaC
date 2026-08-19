import atexit as _atexit

from ._version import __version__
from .client import LazyOpenSACClient as _LazyOpenSACClient
from .transport import BrokerError

sdk = _LazyOpenSACClient()
_atexit.register(sdk.close)

__all__ = [
    "BrokerError",
    "sdk",
    "__version__",
]
