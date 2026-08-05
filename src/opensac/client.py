from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx


class OpenSAC:
    def __init__(self, *, base_url: str = "http://127.0.0.1:8000", api_key: str = "") -> None:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = httpx.Client(base_url=base_url, headers=headers, timeout=60)

    def create_session(
        self,
        *,
        backends: list[str] | None = None,
        limits: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        response = self._client.post(
            "/v1/sessions",
            json={"backends": backends or ["web", "local"], "limits": limits or {}},
        )
        response.raise_for_status()
        return response.json()

    def create_run(self, session_id: str, input: str, **options: Any) -> dict[str, Any]:
        response = self._client.post(
            f"/v1/sessions/{session_id}/runs",
            json={"input": input, **options},
        )
        response.raise_for_status()
        return response.json()

    def get_run(self, run_id: str) -> dict[str, Any]:
        response = self._client.get(f"/v1/runs/{run_id}")
        response.raise_for_status()
        return response.json()

    def create_and_wait(
        self,
        session_id: str,
        input: str,
        *,
        poll_interval: float = 1.0,
        **options: Any,
    ) -> dict[str, Any]:
        run = self.create_run(session_id, input, **options)
        while run["status"] in {"queued", "running"}:
            time.sleep(poll_interval)
            run = self.get_run(run["id"])
        return run

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> OpenSAC:
        return self

    def __exit__(self, *_) -> None:
        self.close()


class AsyncOpenSAC:
    def __init__(self, *, base_url: str = "http://127.0.0.1:8000", api_key: str = "") -> None:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = httpx.AsyncClient(base_url=base_url, headers=headers, timeout=60)

    async def create_session(
        self,
        *,
        backends: list[str] | None = None,
        limits: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        response = await self._client.post(
            "/v1/sessions",
            json={"backends": backends or ["web", "local"], "limits": limits or {}},
        )
        response.raise_for_status()
        return response.json()

    async def create_run(self, session_id: str, input: str, **options: Any) -> dict[str, Any]:
        response = await self._client.post(
            f"/v1/sessions/{session_id}/runs",
            json={"input": input, **options},
        )
        response.raise_for_status()
        return response.json()

    async def get_run(self, run_id: str) -> dict[str, Any]:
        response = await self._client.get(f"/v1/runs/{run_id}")
        response.raise_for_status()
        return response.json()

    async def create_and_wait(
        self,
        session_id: str,
        input: str,
        *,
        poll_interval: float = 1.0,
        **options: Any,
    ) -> dict[str, Any]:
        run = await self.create_run(session_id, input, **options)
        while run["status"] in {"queued", "running"}:
            await asyncio.sleep(poll_interval)
            run = await self.get_run(run["id"])
        return run

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> AsyncOpenSAC:
        return self

    async def __aexit__(self, *_) -> None:
        await self.close()
