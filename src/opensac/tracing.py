"""Host-controlled recording of normalized SDK operations."""

from __future__ import annotations

import inspect
import json
import os
import threading
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from typing import Any, ParamSpec, TypeVar, cast

_SCHEMA_VERSION = 1
_PROCESS_ACTION_ID = uuid.uuid4().hex
_P = ParamSpec("_P")
_T = TypeVar("_T")


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


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


def traced_operation(
    operation: str,
) -> Callable[[Callable[_P, Awaitable[_T]]], Callable[_P, Awaitable[_T]]]:
    """Record an SDK operation's normalized inputs and returned value."""

    def decorate(method: Callable[_P, Awaitable[_T]]) -> Callable[_P, Awaitable[_T]]:
        signature = inspect.signature(method)

        @wraps(method)
        async def call(*args: _P.args, **kwargs: _P.kwargs) -> _T:
            instance = cast(Any, args[0])
            if not instance._tracer.enabled:
                return await method(*args, **kwargs)
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            inputs = {key: value for key, value in bound.arguments.items() if key != "self"}
            call_id = uuid.uuid4().hex
            started_at = datetime.now(UTC).isoformat()
            started = time.perf_counter()
            try:
                result = await method(*args, **kwargs)
            except BaseException as exc:
                instance._tracer.record(
                    call_id=call_id,
                    operation=operation,
                    started_at=started_at,
                    duration_ms=round((time.perf_counter() - started) * 1000, 3),
                    inputs=inputs,
                    error=exc,
                )
                raise
            instance._tracer.record(
                call_id=call_id,
                operation=operation,
                started_at=started_at,
                duration_ms=round((time.perf_counter() - started) * 1000, 3),
                inputs=inputs,
                output=result,
            )
            return result

        return call

    return decorate
