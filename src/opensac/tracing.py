"""Host-controlled recording of normalized SDK operations."""

from __future__ import annotations

import inspect
import json
import math
import os
import threading
import time
import uuid
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from typing import Any, ParamSpec, TypeVar, cast

_SCHEMA_VERSION = 2
_PROCESS_ACTION_ID = uuid.uuid4().hex
_P = ParamSpec("_P")
_T = TypeVar("_T")


def _jsonable(value: Any, ancestors: frozenset[int] = frozenset()) -> Any:
    """Preserve JSON values; mark unsupported/cyclic values without calling repr."""
    marker = {"$unserializable": type(value).__name__}
    if id(value) in ancestors:
        return marker
    ancestors = ancestors | {id(value)}
    if hasattr(value, "model_dump"):
        try:
            return _jsonable(value.model_dump(mode="json", warnings=False), ancestors)
        except Exception:
            return marker
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            return marker
        return {key: _jsonable(item, ancestors) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item, ancestors) for item in value]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    return marker


class Tracer:
    """Append one JSONL record per completed or failed SDK operation."""

    def __init__(
        self,
        *,
        trace_dir: Path | None,
        run_id: str | None,
        action_id: str | None,
    ) -> None:
        self.enabled = trace_dir is not None
        self.run_id = run_id
        self.action_id = action_id or (_PROCESS_ACTION_ID if self.enabled else None)
        self._trace_dir = trace_dir
        self._stream: Any | None = None
        self._lock = threading.Lock()
        self._closed = False

    def record(
        self,
        *,
        call_id: str,
        operation: str,
        started_at: str,
        duration_ms: float,
        inputs: dict[str, Any],
        output: Any = None,
        error: BaseException | None = None,
        layer: str = "runtime",
    ) -> None:
        try:
            event: dict[str, Any] = {
                "schema_version": _SCHEMA_VERSION,
                "event": "operation.call",
                "timestamp": datetime.now(UTC).isoformat(),
                "started_at": started_at,
                "duration_ms": duration_ms,
                "run_id": self.run_id,
                "action_id": self.action_id,
                "process_id": os.getpid(),
                "call_id": call_id,
                "operation": operation,
                "layer": layer,
                "input": _jsonable(inputs),
                "status": "error" if error is not None else "ok",
            }
            if error is None:
                event["output"] = _jsonable(output)
            else:
                event["error_type"] = type(error).__name__
                code = getattr(error, "code", None)
                if isinstance(code, str):
                    event["error_code"] = code
            self._write(event)
        except Exception:
            pass

    def _write(self, event: dict[str, Any]) -> None:
        try:
            encoded = json.dumps(event, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            with self._lock:
                if self._closed:
                    return
                if self._stream is None:
                    assert self._trace_dir is not None
                    self._trace_dir.mkdir(parents=True, exist_ok=True)
                    self._stream = (self._trace_dir / f"opensac-{os.getpid()}.jsonl").open(
                        "a", encoding="utf-8"
                    )
                self._stream.write(f"{encoded}\n")
                self._stream.flush()
        except Exception:
            pass

    def close(self) -> None:
        with self._lock:
            self._closed = True
            if self._stream is not None:
                with suppress(Exception):
                    self._stream.close()
                self._stream = None


@dataclass
class _Call:
    tracer: Tracer
    inputs: dict[str, Any]
    output: Any = None
    normalized: bool = False
    call_id: str = field(default_factory=lambda: uuid.uuid4().hex)


_CURRENT_CALL: ContextVar[_Call | None] = ContextVar("opensac_trace_call", default=None)


def normalized_trace_inputs(tracer: Tracer, inputs: dict[str, Any]) -> None:
    """Update matching entry-point arguments after Runtime argument validation."""
    call = _CURRENT_CALL.get()
    if call is not None and call.tracer is tracer and not call.normalized:
        call.normalized = True
        call.inputs.update({key: value for key, value in inputs.items() if key in call.inputs})


def _inputs(
    signature: inspect.Signature, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> dict[str, Any]:
    try:
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        return {key: value for key, value in bound.arguments.items() if key != "self"}
    except TypeError:
        # Invalid call signatures still count as failed calls.
        return {"args": args[1:], "kwargs": kwargs}


@contextmanager
def _trace_call(
    tracer: Tracer, operation: str, inputs: dict[str, Any], *, layer: str
) -> Iterator[_Call]:
    call = _Call(tracer, inputs)
    token = _CURRENT_CALL.set(call)
    started_at = datetime.now(UTC).isoformat()
    started = time.perf_counter()
    error: BaseException | None = None
    try:
        yield call
    except BaseException as exc:
        error = exc
        raise
    finally:
        _CURRENT_CALL.reset(token)
        tracer.record(
            call_id=call.call_id,
            operation=operation,
            started_at=started_at,
            duration_ms=round((time.perf_counter() - started) * 1000, 3),
            inputs=call.inputs,
            output=call.output,
            error=error,
            layer=layer,
        )


def traced_operation(
    operation: str,
) -> Callable[[Callable[_P, Awaitable[_T]]], Callable[_P, Awaitable[_T]]]:
    """Record direct Runtime calls, including argument validation failures."""

    def decorate(method: Callable[_P, Awaitable[_T]]) -> Callable[_P, Awaitable[_T]]:
        signature = inspect.signature(method)

        @wraps(method)
        async def call(*args: _P.args, **kwargs: _P.kwargs) -> _T:
            instance = cast(Any, args[0])
            tracer = instance._tracer
            parent = _CURRENT_CALL.get()
            if not tracer.enabled or (parent is not None and parent.tracer is tracer):
                return await method(*args, **kwargs)
            with _trace_call(
                tracer, operation, _inputs(signature, args, kwargs), layer="runtime"
            ) as event:
                result = await method(*args, **kwargs)
                event.output = result
                return result

        return call

    return decorate


def traced_sdk_operation(operation: str) -> Callable[[Callable[_P, _T]], Callable[_P, _T]]:
    """Record the final SDK result once; AnyIO propagates context to its portal."""

    def decorate(method: Callable[_P, _T]) -> Callable[_P, _T]:
        signature = inspect.signature(method)

        @wraps(method)
        def call(*args: _P.args, **kwargs: _P.kwargs) -> _T:
            client = cast(Any, args[0])._client
            client._check_open()
            tracer = client._runtime.tracer
            if not tracer.enabled:
                return method(*args, **kwargs)
            with _trace_call(
                tracer, operation, _inputs(signature, args, kwargs), layer="sdk"
            ) as event:
                result = method(*args, **kwargs)
                event.output = result
                return result

        return call

    return decorate
