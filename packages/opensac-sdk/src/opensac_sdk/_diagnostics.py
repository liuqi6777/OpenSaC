from __future__ import annotations

import json
import os
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ._json import atomic_write_text, strict_json_dumps

_MAX_WARNINGS = 16
_MAX_FAILURES_PER_WARNING = 8
_MAX_WARNING_BYTES = 4_096
_MAX_CONTEXT_CHARS = 512
_MAX_MESSAGE_CHARS = 1_024
_OUTPUT_LOCK = threading.Lock()
_FAILURE_FIELDS = (
    "code",
    "message",
    "retryable",
    "attempts",
    "provider_status",
    "retry_after_seconds",
    "provider",
    "component",
    "scope",
)


def _output_path() -> Path:
    return Path(os.environ.get("OPENSAC_OUTPUT_PATH", "/workspace/.opensac-output.json"))


def _read_envelope(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_envelope(path: Path, envelope: dict[str, Any]) -> None:
    atomic_write_text(
        path,
        strict_json_dumps(envelope, field="execution output", indent=2),
    )


def write_submission(path: Path, output: Any, citations: list[str]) -> None:
    """Atomically replace submitted output while preserving SDK diagnostics."""
    with _OUTPUT_LOCK:
        envelope = _read_envelope(path)
        warnings = envelope.get("warnings")
        replacement: dict[str, Any] = {"output": output, "citations": citations}
        if isinstance(warnings, list) and warnings:
            replacement["warnings"] = warnings
        _write_envelope(path, replacement)


def failure_detail(
    failure: Mapping[str, Any],
    *,
    input_index: int | None = None,
    query: str | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    """Flatten one typed item failure into a bounded agent-visible diagnostic."""
    detail: dict[str, Any] = {}
    if input_index is not None:
        detail["input_index"] = input_index
    if query is not None:
        detail["query"] = query[:_MAX_CONTEXT_CHARS]
    if source is not None:
        detail["source"] = source[:_MAX_CONTEXT_CHARS]
    for field in _FAILURE_FIELDS:
        value = failure.get(field)
        if value is None:
            continue
        if field == "message":
            value = str(value)[:_MAX_MESSAGE_CHARS]
        elif field in {"code", "provider", "component", "scope"}:
            value = str(value)[:_MAX_CONTEXT_CHARS]
        detail[field] = value
    return detail


def record_external_failures(
    method: str,
    *,
    success_count: int,
    failures: list[dict[str, Any]],
) -> None:
    """Persist a bounded warning without changing the capability result."""
    if not failures:
        return
    retained = failures[:_MAX_FAILURES_PER_WARNING]
    warning = {
        "code": "external_result_failure",
        "method": method,
        "success_count": success_count,
        "failure_count": len(failures),
        "failures": retained,
        "omitted_failure_count": len(failures) - len(retained),
    }
    path = _output_path()
    try:
        with _OUTPUT_LOCK:
            envelope = _read_envelope(path)
            warnings = envelope.get("warnings")
            warnings = list(warnings) if isinstance(warnings, list) else []
            if any(
                isinstance(existing, dict)
                and existing.get("success_count") == warning["success_count"]
                and existing.get("failure_count") == warning["failure_count"]
                and existing.get("failures") == warning["failures"]
                for existing in warnings
            ):
                return
            if len(warnings) >= _MAX_WARNINGS:
                return
            candidate = warnings + [warning]
            while (
                len(strict_json_dumps(candidate, field="warnings").encode("utf-8"))
                > _MAX_WARNING_BYTES
            ):
                trimmable = [
                    item
                    for item in candidate
                    if isinstance(item, dict)
                    and isinstance(item.get("failures"), list)
                    and item["failures"]
                ]
                if not trimmable:
                    return
                largest = max(trimmable, key=lambda item: len(item["failures"]))
                largest["failures"].pop()
                largest["omitted_failure_count"] = (
                    int(largest.get("omitted_failure_count") or 0) + 1
                )
            envelope["warnings"] = candidate
            _write_envelope(path, envelope)
    except (OSError, ValueError):
        # Diagnostics must never turn a usable partial result into a program failure.
        return
