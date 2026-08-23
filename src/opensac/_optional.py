from __future__ import annotations

from collections.abc import Sequence
from importlib.util import find_spec


class MissingOptionalDependency(RuntimeError):
    """Raised when an explicitly requested optional integration is not installed."""


def require_extra(feature: str, extra: str, modules: Sequence[str]) -> None:
    missing = [module for module in modules if find_spec(module) is None]
    if not missing:
        return
    packages = ", ".join(missing)
    raise MissingOptionalDependency(
        f"{feature} requires optional dependencies ({packages}); install with 'opensac[{extra}]'."
    )
