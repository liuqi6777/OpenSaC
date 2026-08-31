"""Shared protocol primitives for adapters that expose ``sac_run``."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import httpx

DEFAULT_TIMEOUT_SECONDS = 300.0
DEFAULT_OUTPUT_LIMIT = 32_000
_WARNING_OUTPUT_LIMIT = 4_096
_STATE_LOSS_CODES = frozenset({"session_expired", "worker_restarted", "interpreter_lost"})


class AsyncSessionClient:
    """Policy-free async client for the OpenSAC session REST endpoints."""

    def __init__(
        self,
        *,
        api_base: str,
        api_key: str = "",
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self.http = httpx.AsyncClient(
            base_url=api_base,
            headers=headers,
            timeout=timeout,
            transport=transport,
        )

    async def create_session(self, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        response = await self.http.post("/v1/sessions", json=dict(payload or {}))
        response.raise_for_status()
        return response.json()

    async def exec_code(
        self,
        session_id: str,
        code: str,
        *,
        exec_id: str | None = None,
        include_trace: bool = False,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": code, "include_trace": include_trace}
        if exec_id is not None:
            payload["exec_id"] = exec_id
        response = await self.http.post(
            f"/v1/sessions/{session_id}/exec",
            json=payload,
        )
        response.raise_for_status()
        return response.json()

    async def delete_session(self, session_id: str) -> None:
        response = await self.http.delete(f"/v1/sessions/{session_id}")
        response.raise_for_status()

    async def close(self) -> None:
        await self.http.aclose()


def contract_error_code(response: httpx.Response) -> str | None:
    """Return a stable OpenSAC error-contract code, if present."""
    try:
        payload = response.json()
        detail = payload.get("detail") if isinstance(payload, Mapping) else None
        detail = detail or {}
        code = detail.get("code") if isinstance(detail, Mapping) else None
    except (TypeError, ValueError):
        return None
    return str(code) if isinstance(code, str) and code else None


def state_loss_code(response: httpx.Response) -> str | None:
    """Return the stable OpenSAC loss code for a 410 response, if present."""
    if response.status_code != 410:
        return None
    code = contract_error_code(response)
    return code if code in _STATE_LOSS_CODES else None


def render_observation(
    payload: Mapping[str, Any], *, output_limit: int = DEFAULT_OUTPUT_LIMIT
) -> str:
    sections: list[str] = []
    warning_body = _render_warnings(payload.get("warnings"))
    stderr = str(payload.get("stderr") or "").strip()
    error = _render_error(payload, stderr=stderr)
    if stderr and not error:
        stderr_warning = _tagged("warning", {"code": "stderr_output", "message": stderr})
        warning_body = f"{warning_body}\n{stderr_warning}" if warning_body else stderr_warning
    if warning_body:
        sections.append(truncate_observation(warning_body, _WARNING_OUTPUT_LIMIT))

    stdout = "" if payload.get("stdout") is None else str(payload["stdout"])
    if stdout:
        sections.append(stdout)
    if error:
        sections.append(error)

    if sections == [stdout]:
        return truncate_observation(stdout, output_limit)
    rendered = "\n\n".join(section.strip() for section in sections if section.strip())
    return truncate_observation(rendered, output_limit) if rendered else ""


def _render_warnings(value: Any) -> str:
    if not isinstance(value, list):
        return ""
    lines: list[str] = []
    for warning in value:
        if not isinstance(warning, Mapping):
            continue
        lines.append(
            _tagged(
                "warning",
                {
                    "code": str(warning.get("code") or "external_result_failure"),
                    "method": str(warning.get("method") or "unknown"),
                    "success_count": int(warning.get("success_count") or 0),
                    "failure_count": int(warning.get("failure_count") or 0),
                    "omitted_failure_count": int(warning.get("omitted_failure_count") or 0),
                },
            )
        )
        failures = warning.get("failures")
        if isinstance(failures, list):
            for failure in failures:
                if not isinstance(failure, Mapping):
                    continue
                detail = {
                    field: failure[field]
                    for field in (
                        "code",
                        "input_index",
                        "source",
                        "query",
                        "retryable",
                        "attempts",
                        "provider_status",
                        "retry_after_seconds",
                        "provider",
                        "component",
                        "scope",
                        "message",
                    )
                    if failure.get(field) is not None
                }
                detail.setdefault("code", "unknown")
                detail.setdefault("message", "External operation failed.")
                lines.append(_tagged("warning", detail))
    return "\n".join(lines)


def _render_error(payload: Mapping[str, Any], *, stderr: str) -> str:
    error = payload.get("error")
    if error:
        return render_error("sandbox_error", str(error))
    if payload.get("interpreter_state") == "lost":
        return render_error(
            "state_lost",
            "The persistent interpreter was lost; the cell will not be replayed.",
            reason=str(payload.get("interpreter_loss_reason") or "unknown"),
        )
    if payload.get("timed_out"):
        return render_error("timed_out", "Execution timed out.")
    if payload.get("output_limit_exceeded"):
        return render_error(
            "output_limit_exceeded",
            "Execution output exceeded its limit.",
        )
    if payload.get("succeeded") is False:
        return render_error(
            "execution_failed",
            stderr or f"Program exited with code {payload.get('exit_code')}.",
            exit_code=payload.get("exit_code"),
        )
    return ""


def render_error(code: str, message: str, **details: Any) -> str:
    value = {"code": code, **details, "message": message}
    return _tagged("error", value)


def _tagged(kind: str, value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    return f"[OpenSAC {kind}] {encoded}"


def truncate_observation(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    marker = f"\n... [{len(text) - limit} chars elided] ...\n"
    budget = max(0, limit - len(marker))
    head = budget // 3
    tail = budget - head
    return text[:head] + marker + text[-tail:] if tail else text[:head] + marker
