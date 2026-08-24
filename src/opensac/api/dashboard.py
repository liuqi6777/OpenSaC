from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse, StreamingResponse

from opensac.models import CapabilityEvent

if TYPE_CHECKING:
    from opensac.api.runtime import ApplicationRuntime

Authorize = Callable[..., Awaitable[None]]

CODE_PREVIEW_BYTES = 64 * 1024
VALUE_PREVIEW_BYTES = 32 * 1024
ERROR_PREVIEW_BYTES = 4 * 1024
MAX_ACTIVE_CAPABILITY_HISTORY = 50
SUBSCRIBER_QUEUE_SIZE = 1_024

_ASSETS = {
    "app.js": "application/javascript; charset=utf-8",
    "styles.css": "text/css; charset=utf-8",
}
_CSP = (
    "default-src 'none'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "connect-src 'self'; "
    "img-src 'self' data:; "
    "font-src 'self'; "
    "base-uri 'none'; "
    "form-action 'none'; "
    "frame-ancestors 'none'"
)


def _utc_iso() -> str:
    return datetime.now(UTC).isoformat()


def _asset_bytes(name: str) -> bytes:
    packaged = resources.files("opensac").joinpath("_dashboard_assets").joinpath(name)
    if packaged.is_file():
        return packaged.read_bytes()

    # Editable installs may expose src/ directly without applying Hatch's wheel mapping.
    source = Path(__file__).resolve().parents[3] / "dashboard" / name
    if source.is_file():
        return source.read_bytes()
    raise FileNotFoundError(name)


def _bounded_value(value: Any, *, depth: int = 0) -> Any:
    """Bound nested values before JSON encoding so previews cannot amplify memory use."""

    if depth >= 6:
        return "[maximum depth reached]"
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        encoded = value.encode("utf-8")
        if len(encoded) <= VALUE_PREVIEW_BYTES:
            return value
        return encoded[:VALUE_PREVIEW_BYTES].decode("utf-8", errors="ignore") + "…"
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if isinstance(value, dict):
        items = list(value.items())
        bounded = {str(key): _bounded_value(item, depth=depth + 1) for key, item in items[:50]}
        if len(items) > 50:
            bounded["[omitted]"] = f"{len(items) - 50} additional fields"
        return bounded
    if isinstance(value, list | tuple | set):
        items = list(value)
        bounded_items = [_bounded_value(item, depth=depth + 1) for item in items[:50]]
        if len(items) > 50:
            bounded_items.append(f"[{len(items) - 50} additional items omitted]")
        return bounded_items
    return _bounded_value(str(value), depth=depth + 1)


@dataclass(slots=True)
class _ActiveExecution:
    task_id: str
    session_id: str
    exec_id: str | None
    execution_mode: str
    code: dict[str, Any]
    started_at: str
    started_monotonic: float
    phase: str = "session_queue"
    phase_started_monotonic: float = field(default_factory=time.monotonic)
    internal_execution_id: str | None = None
    active_capabilities: dict[int, dict[str, Any]] = field(default_factory=dict)
    capabilities: list[dict[str, Any]] = field(default_factory=list)
    omitted_capabilities: int = 0


class DashboardTelemetry:
    """Process-local, non-persistent telemetry for the built-in dashboard."""

    def __init__(self, *, enabled: bool, secrets: list[str]) -> None:
        self.enabled = enabled
        self._secrets = sorted({value for value in secrets if value}, key=len, reverse=True)
        self._active: dict[str, _ActiveExecution] = {}
        self._execution_ids: dict[str, str] = {}
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._event_id = 0
        self._counters = {
            "started": 0,
            "completed": 0,
            "succeeded": 0,
            "failed": 0,
            "cancelled": 0,
            "timed_out": 0,
            "output_limit_exceeded": 0,
        }

    def _redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "[REDACTED]")
        return text

    def preview(self, value: Any, limit: int, *, plain: bool = False) -> dict[str, Any]:
        if plain:
            text = str(value)
        else:
            text = json.dumps(
                _bounded_value(value),
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        text = self._redact(text)
        encoded = text.encode("utf-8")
        truncated = len(encoded) > limit
        if truncated:
            text = encoded[:limit].decode("utf-8", errors="ignore")
        return {
            "text": text,
            "truncated": truncated,
            "original_bytes": len(encoded),
        }

    def envelope(self, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        self._event_id += 1
        return {
            "id": self._event_id,
            "type": event_type,
            "at": _utc_iso(),
            "payload": payload,
        }

    def _publish(self, event_type: str, payload: dict[str, Any]) -> None:
        if not self.enabled or not self._subscribers:
            return
        event = self.envelope(event_type, payload)
        for queue in tuple(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait(
                    self.envelope(
                        "gap",
                        {"reason": "subscriber_queue_overflow", "resync": True},
                    )
                )

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_SIZE)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(queue)

    def _public_execution(self, active: _ActiveExecution) -> dict[str, Any]:
        now = time.monotonic()
        return {
            "task_id": active.task_id,
            "session_id": active.session_id,
            "exec_id": active.exec_id,
            "execution_mode": active.execution_mode,
            "phase": active.phase,
            "started_at": active.started_at,
            "elapsed_seconds": max(0.0, now - active.started_monotonic),
            "phase_elapsed_seconds": max(0.0, now - active.phase_started_monotonic),
            "code": active.code,
            "active_capabilities": sorted(
                active.active_capabilities.values(), key=lambda item: item["sequence"]
            ),
            "capabilities": list(active.capabilities),
            "omitted_capabilities": active.omitted_capabilities,
        }

    def snapshot(self, health: dict[str, Any]) -> dict[str, Any]:
        return {
            "version": 1,
            "generated_at": _utc_iso(),
            "health": health,
            "counters": dict(self._counters),
            "executions": [
                self._public_execution(active)
                for active in sorted(self._active.values(), key=lambda item: item.started_monotonic)
            ],
        }

    def start_execution(
        self,
        *,
        session_id: str,
        exec_id: str | None,
        execution_mode: str,
        code: str,
    ) -> str | None:
        if not self.enabled:
            return None
        task_id = f"dash_{uuid.uuid4().hex}"
        active = _ActiveExecution(
            task_id=task_id,
            session_id=session_id,
            exec_id=exec_id,
            execution_mode=execution_mode,
            code=self.preview(code, CODE_PREVIEW_BYTES, plain=True),
            started_at=_utc_iso(),
            started_monotonic=time.monotonic(),
        )
        self._active[task_id] = active
        self._counters["started"] += 1
        self._publish("exec.started", {"execution": self._public_execution(active)})
        return task_id

    def set_phase(self, task_id: str | None, phase: str) -> None:
        if task_id is None or (active := self._active.get(task_id)) is None:
            return
        active.phase = phase
        active.phase_started_monotonic = time.monotonic()
        self._publish(
            "exec.phase",
            {
                "task_id": task_id,
                "phase": phase,
                "elapsed_seconds": time.monotonic() - active.started_monotonic,
            },
        )

    def bind_execution(self, task_id: str | None, execution_id: str) -> None:
        if task_id is None or (active := self._active.get(task_id)) is None:
            return
        active.internal_execution_id = execution_id
        self._execution_ids[execution_id] = task_id

    def capability_started(
        self,
        execution_id: str,
        sequence: int,
        method: str,
        params: dict[str, Any],
    ) -> None:
        task_id = self._execution_ids.get(execution_id)
        if task_id is None or (active := self._active.get(task_id)) is None:
            return
        capability = {
            "sequence": sequence,
            "method": method,
            "status": "running",
            "started_at": _utc_iso(),
            "params": self.preview(params, VALUE_PREVIEW_BYTES),
        }
        active.active_capabilities[sequence] = capability
        self._publish(
            "capability.started",
            {"task_id": task_id, "capability": capability},
        )

    def capability_completed(
        self,
        execution_id: str,
        sequence: int,
        event: CapabilityEvent,
        result: Any,
    ) -> None:
        task_id = self._execution_ids.get(execution_id)
        if task_id is None or (active := self._active.get(task_id)) is None:
            return
        started = active.active_capabilities.pop(sequence, None)
        capability = {
            "sequence": sequence,
            "method": event.method,
            "status": event.status,
            "started_at": started.get("started_at") if started else None,
            "duration_seconds": event.duration_seconds,
            "params": started.get("params") if started else None,
            "queries": self.preview(event.queries, VALUE_PREVIEW_BYTES),
            "input_count": event.input_count,
            "result_count": event.result_count,
            "model_tokens": event.model_tokens,
            "provider_attempts": [
                attempt.model_dump(mode="json") for attempt in event.provider_attempts
            ],
            "model_attempts": [attempt.model_dump(mode="json") for attempt in event.model_attempts],
            "provider_cache_hits": event.provider_cache_hits,
            "provider_cache_misses": event.provider_cache_misses,
            "error_type": event.error_type,
            "error": (
                self.preview(event.error, ERROR_PREVIEW_BYTES, plain=True) if event.error else None
            ),
            "result": (self.preview(result, VALUE_PREVIEW_BYTES) if event.status == "ok" else None),
        }
        active.capabilities.append(capability)
        if len(active.capabilities) > MAX_ACTIVE_CAPABILITY_HISTORY:
            active.capabilities.pop(0)
            active.omitted_capabilities += 1
        self._publish(
            "capability.completed",
            {"task_id": task_id, "capability": capability},
        )

    def complete_execution(
        self,
        task_id: str | None,
        *,
        result: Any = None,
        error: BaseException | None = None,
        cancelled: bool = False,
    ) -> None:
        if task_id is None or (active := self._active.get(task_id)) is None:
            return
        self._counters["completed"] += 1
        succeeded = bool(result is not None and result.succeeded)
        self._counters["succeeded" if succeeded else "failed"] += 1
        if cancelled:
            self._counters["cancelled"] += 1
        if result is not None and result.timed_out:
            self._counters["timed_out"] += 1
        if result is not None and result.output_limit_exceeded:
            self._counters["output_limit_exceeded"] += 1

        payload: dict[str, Any] = {
            "task_id": task_id,
            "session_id": active.session_id,
            "exec_id": active.exec_id,
            "execution_mode": active.execution_mode,
            "started_at": active.started_at,
            "finished_at": _utc_iso(),
            "elapsed_seconds": time.monotonic() - active.started_monotonic,
            "succeeded": succeeded,
            "cancelled": cancelled,
            "code": active.code,
            "capabilities": list(active.capabilities),
            "omitted_capabilities": active.omitted_capabilities,
        }
        if result is not None:
            payload.update(
                {
                    "exit_code": result.exit_code,
                    "timed_out": result.timed_out,
                    "output_limit_exceeded": result.output_limit_exceeded,
                    "stdout": self.preview(result.stdout, VALUE_PREVIEW_BYTES, plain=True),
                    "stderr": self.preview(result.stderr, VALUE_PREVIEW_BYTES, plain=True),
                    "output": self.preview(result.output, VALUE_PREVIEW_BYTES),
                    "error": (
                        self.preview(result.error, ERROR_PREVIEW_BYTES, plain=True)
                        if result.error
                        else None
                    ),
                    "timings": dict(result.timings),
                    "usage": result.usage.model_dump(mode="json"),
                    "artifacts": list(result.artifacts),
                    "citations": self.preview(result.citations, VALUE_PREVIEW_BYTES),
                }
            )
        elif error is not None:
            payload.update(
                {
                    "error_type": type(error).__name__,
                    "error": self.preview(error, ERROR_PREVIEW_BYTES, plain=True),
                }
            )

        self._publish("exec.completed", payload)
        self._active.pop(task_id, None)
        if active.internal_execution_id is not None:
            self._execution_ids.pop(active.internal_execution_id, None)


def _sse_message(event: dict[str, Any]) -> bytes:
    payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    return (f"id: {event['id']}\nevent: {event['type']}\ndata: {payload}\n\n").encode()


async def dashboard_event_stream(
    runtime: ApplicationRuntime,
    request: Request,
) -> AsyncIterator[bytes]:
    telemetry = runtime.dashboard
    queue = telemetry.subscribe()
    next_metrics = time.monotonic() + 1.0
    try:
        yield _sse_message(telemetry.envelope("snapshot", runtime.dashboard_snapshot()))
        while not await request.is_disconnected():
            now = time.monotonic()
            if now >= next_metrics:
                yield _sse_message(telemetry.envelope("metrics", runtime.dashboard_snapshot()))
                next_metrics = now + 1.0
                continue
            try:
                event = await asyncio.wait_for(queue.get(), timeout=next_metrics - now)
            except TimeoutError:
                continue
            yield _sse_message(event)
    finally:
        telemetry.unsubscribe(queue)


def create_dashboard_router(
    runtime: ApplicationRuntime,
    authorize: Authorize,
) -> APIRouter:
    router = APIRouter(include_in_schema=False)
    protected = APIRouter(dependencies=[Depends(authorize)], include_in_schema=False)

    @router.get("/dashboard")
    async def dashboard_redirect() -> RedirectResponse:
        # Keep the Location relative so a reverse-proxy prefix remains in the browser URL.
        return RedirectResponse(url="dashboard/", status_code=307)

    @router.get("/dashboard/")
    async def dashboard_index() -> Response:
        try:
            content = _asset_bytes("index.html")
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Dashboard assets are unavailable") from exc
        return Response(
            content=content,
            media_type="text/html",
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": _CSP,
                "X-Content-Type-Options": "nosniff",
            },
        )

    @router.get("/dashboard/assets/{asset_name}")
    async def dashboard_asset(asset_name: str) -> Response:
        media_type = _ASSETS.get(asset_name)
        if media_type is None:
            raise HTTPException(status_code=404, detail="Dashboard asset not found")
        try:
            content = _asset_bytes(asset_name)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Dashboard asset not found") from exc
        return Response(
            content=content,
            media_type=media_type,
            headers={
                "Cache-Control": "public, max-age=3600",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @protected.get("/dashboard/api/snapshot")
    async def dashboard_snapshot() -> dict[str, Any]:
        return runtime.dashboard_snapshot()

    @protected.get("/dashboard/api/events")
    async def dashboard_events(request: Request) -> StreamingResponse:
        return StreamingResponse(
            dashboard_event_stream(runtime, request),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    router.include_router(protected)
    return router


__all__ = [
    "DashboardTelemetry",
    "create_dashboard_router",
    "dashboard_event_stream",
]
