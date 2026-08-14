"""Shared protocol primitives for adapters that expose ``sac_run``."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import httpx

DEFAULT_TIMEOUT_SECONDS = 300.0
DEFAULT_OUTPUT_LIMIT = 32_000
_STATE_LOSS_CODES = frozenset({"session_expired", "worker_restarted"})


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

    async def create_session(
        self, payload: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        response = await self.http.post("/v1/sessions", json=dict(payload or {}))
        response.raise_for_status()
        return response.json()

    async def exec_code(
        self,
        session_id: str,
        code: str,
        *,
        include_trace: bool = False,
    ) -> dict[str, Any]:
        response = await self.http.post(
            f"/v1/sessions/{session_id}/exec",
            json={"code": code, "include_trace": include_trace},
        )
        response.raise_for_status()
        return response.json()

    async def delete_session(self, session_id: str) -> None:
        response = await self.http.delete(f"/v1/sessions/{session_id}")
        response.raise_for_status()

    async def close(self) -> None:
        await self.http.aclose()


def state_loss_code(response: httpx.Response) -> str | None:
    """Return the stable OpenSAC loss code for a 410 response, if present."""
    if response.status_code != 410:
        return None
    try:
        payload = response.json()
        detail = payload.get("detail") if isinstance(payload, Mapping) else None
        detail = detail or {}
        code = detail.get("code") if isinstance(detail, dict) else None
    except (TypeError, ValueError):
        return None
    return str(code) if code in _STATE_LOSS_CODES else None


def render_observation(
    payload: Mapping[str, Any], *, output_limit: int = DEFAULT_OUTPUT_LIMIT
) -> str:
    if payload.get("error"):
        return f"[sac_run] {payload['error']}"

    usage = payload.get("usage") or {}
    sections = [
        f"[sac_run] exit_code={payload.get('exit_code')} "
        f"duration={float(payload.get('duration_seconds', 0.0)):.1f}s "
        f"search_calls={usage.get('search_calls', 0)} "
        f"docs_fetched={usage.get('content_fetches', 0)}"
    ]
    bodies: list[tuple[str, str]] = []
    if str(payload.get("stdout") or "").strip():
        bodies.append(("stdout", str(payload["stdout"]).strip()))
    if str(payload.get("stderr") or "").strip():
        bodies.append(("stderr", str(payload["stderr"]).strip()))
    if payload.get("output") is not None:
        bodies.append(
            (
                "submitted output",
                json.dumps(payload["output"], ensure_ascii=False, default=str),
            )
        )

    remaining = output_limit
    for label, body in bodies:
        if remaining <= 0:
            break
        rendered = truncate_observation(body, remaining)
        sections.append(f"{label}:\n{rendered}")
        remaining -= len(rendered)

    citations = payload.get("citations") or []
    if citations:
        sections.append(f"resolved citations: {len(citations)}")
    artifacts = sorted(str(item) for item in (payload.get("artifacts") or []))
    sections.append(
        "workspace: empty"
        if not artifacts
        else f"workspace: {len(artifacts)} file(s): {', '.join(artifacts[:40])}"
    )
    if len(sections) == 2 and not artifacts:
        sections.insert(1, "The program printed and submitted nothing.")
    return "\n\n".join(sections)


def truncate_observation(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    marker = f"\n... [{len(text) - limit} chars elided] ...\n"
    budget = max(0, limit - len(marker))
    head = budget // 3
    tail = budget - head
    return text[:head] + marker + text[-tail:] if tail else text[:head] + marker
