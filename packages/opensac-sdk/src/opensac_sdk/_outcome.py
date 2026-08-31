from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from ._diagnostics import error_info, failure_detail, record_external_failures
from ._record import Record, wrap
from .transport import BrokerError

_RESERVED_FIELDS = frozenset({"status", "value", "error"})


class _Success[ValueT](Record):
    def __init__(self, value: ValueT, *, context: Mapping[str, Any] | None = None) -> None:
        super().__init__()
        self.update(_wrapped_context(context))
        self.update({"status": "success", "value": wrap(value), "error": None})


class _Failure(Record):
    def __init__(
        self,
        error: Mapping[str, Any] | BaseException,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__()
        self.update(_wrapped_context(context))
        self.update({"status": "failure", "value": None, "error": wrap(error_info(error))})


type Outcome[ValueT] = _Success[ValueT] | _Failure


def success[ValueT](
    value: ValueT,
    *,
    context: Mapping[str, Any] | None = None,
) -> Outcome[ValueT]:
    return _Success(value, context=context)


def failure(
    error: Mapping[str, Any] | BaseException,
    *,
    context: Mapping[str, Any] | None = None,
) -> _Failure:
    return _Failure(error, context=context)


def capture[ValueT](
    method: str,
    call: Callable[[], ValueT],
    *,
    context: Mapping[str, Any] | None = None,
    input_index: int | None = None,
    query: str | None = None,
    source: str | None = None,
) -> Outcome[ValueT]:
    """Return one outcome and make an external failure visible to the host renderer."""

    try:
        return success(call(), context=context)
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
        return failure(info, context=context)


def _wrapped_context(context: Mapping[str, Any] | None) -> dict[str, Any]:
    if not context:
        return {}
    overlap = _RESERVED_FIELDS.intersection(context)
    if overlap:
        names = ", ".join(sorted(overlap))
        raise RuntimeError(f"outcome context contains reserved field(s): {names}")
    return {key: wrap(value) for key, value in context.items()}
