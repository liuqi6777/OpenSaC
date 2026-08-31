from __future__ import annotations

from collections.abc import Callable

from ._diagnostics import error_info, failure_detail, record_external_failures
from .transport import BrokerError


def capture_optional[ValueT](
    method: str,
    call: Callable[[], ValueT],
    *,
    input_index: int | None = None,
    query: str | None = None,
    source: str | None = None,
) -> ValueT | None:
    """Return a capability result or record one operational failure."""

    try:
        return call()
    except BrokerError as error:
        info = error_info(error)
        record_external_failures(
            method,
            success_count=0,
            failures=[
                failure_detail(
                    info,
                    input_index=input_index,
                    query=query,
                    source=source,
                )
            ],
        )
        return None
