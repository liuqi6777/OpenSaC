from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx


class OpenSAC:
    def __init__(
        self,
        *,
        base_url: str = "http://127.0.0.1:8000",
        api_key: str = "",
        timeout: float = 300.0,
    ) -> None:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = httpx.Client(base_url=base_url, headers=headers, timeout=timeout)
        self._session_features: dict[str, frozenset[str]] = {}

    def create_session(
        self,
        *,
        backends: list[str] | None = None,
        limits: dict[str, Any] | None = None,
        mechanisms: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        response = self._client.post(
            "/v1/sessions",
            json={
                "backends": ["local"] if backends is None else backends,
                "limits": limits or {},
                "mechanisms": mechanisms or {},
            },
        )
        response.raise_for_status()
        session = response.json()
        self._remember_features(session)
        return session

    def health(self) -> dict[str, Any]:
        response = self._client.get("/healthz")
        response.raise_for_status()
        return response.json()

    def _remember_features(self, session: dict[str, Any]) -> frozenset[str]:
        features = frozenset(str(item) for item in session.get("features", []))
        self._session_features[str(session["id"])] = features
        return features

    def _features_for_session(self, session_id: str) -> frozenset[str]:
        features = self._session_features.get(session_id)
        if features is not None:
            return features
        response = self._client.get(f"/v1/sessions/{session_id}")
        response.raise_for_status()
        return self._remember_features(response.json())

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

    def exec_code(
        self,
        session_id: str,
        code: str,
        *,
        exec_id: str | None = None,
        include_trace: bool = False,
    ) -> dict[str, Any]:
        """Run one program you generated yourself, instead of delegating to
        OpenSAC's control model via `create_run`."""
        payload: dict[str, Any] = {"code": code, "include_trace": include_trace}
        if exec_id is not None:
            if "idempotent_exec" not in self._features_for_session(session_id):
                raise RuntimeError(
                    "The server does not advertise idempotent_exec; refusing exec_id"
                )
            payload["exec_id"] = exec_id
        response = self._client.post(f"/v1/sessions/{session_id}/exec", json=payload)
        response.raise_for_status()
        return response.json()

    def delete_session(self, session_id: str) -> None:
        self._client.delete(f"/v1/sessions/{session_id}").raise_for_status()
        self._session_features.pop(session_id, None)

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
    def __init__(
        self,
        *,
        base_url: str = "http://127.0.0.1:8000",
        api_key: str = "",
        timeout: float = 300.0,
    ) -> None:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = httpx.AsyncClient(base_url=base_url, headers=headers, timeout=timeout)
        self._session_features: dict[str, frozenset[str]] = {}

    async def create_session(
        self,
        *,
        backends: list[str] | None = None,
        limits: dict[str, Any] | None = None,
        mechanisms: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        response = await self._client.post(
            "/v1/sessions",
            json={
                "backends": ["local"] if backends is None else backends,
                "limits": limits or {},
                "mechanisms": mechanisms or {},
            },
        )
        response.raise_for_status()
        session = response.json()
        self._remember_features(session)
        return session

    async def health(self) -> dict[str, Any]:
        response = await self._client.get("/healthz")
        response.raise_for_status()
        return response.json()

    def _remember_features(self, session: dict[str, Any]) -> frozenset[str]:
        features = frozenset(str(item) for item in session.get("features", []))
        self._session_features[str(session["id"])] = features
        return features

    async def _features_for_session(self, session_id: str) -> frozenset[str]:
        features = self._session_features.get(session_id)
        if features is not None:
            return features
        response = await self._client.get(f"/v1/sessions/{session_id}")
        response.raise_for_status()
        return self._remember_features(response.json())

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

    async def exec_code(
        self,
        session_id: str,
        code: str,
        *,
        exec_id: str | None = None,
        include_trace: bool = False,
    ) -> dict[str, Any]:
        """Run one program you generated yourself, instead of delegating to
        OpenSAC's control model via `create_run`."""
        payload: dict[str, Any] = {"code": code, "include_trace": include_trace}
        if exec_id is not None:
            if "idempotent_exec" not in await self._features_for_session(session_id):
                raise RuntimeError(
                    "The server does not advertise idempotent_exec; refusing exec_id"
                )
            payload["exec_id"] = exec_id
        response = await self._client.post(
            f"/v1/sessions/{session_id}/exec", json=payload
        )
        response.raise_for_status()
        return response.json()

    async def delete_session(self, session_id: str) -> None:
        (await self._client.delete(f"/v1/sessions/{session_id}")).raise_for_status()
        self._session_features.pop(session_id, None)

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
