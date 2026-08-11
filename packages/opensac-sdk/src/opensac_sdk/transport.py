from __future__ import annotations

import os
import threading
from typing import Any

import httpx

from .models import RpcError, RpcRequest, RpcResponse


class BrokerError(RuntimeError):
    def __init__(self, message: str, *, code: str, retryable: bool) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class UnixSocketTransport:
    def __init__(self, socket_path: str, session_token: str, timeout: float = 60.0) -> None:
        self.socket_path = socket_path
        self.session_token = session_token
        self.timeout = timeout
        self._client: httpx.Client | None = None
        self._client_lock = threading.Lock()

    @classmethod
    def from_environment(cls) -> UnixSocketTransport:
        socket_path = os.environ.get("OPENSAC_BROKER_SOCKET")
        session_token = os.environ.get("OPENSAC_SESSION_TOKEN")
        if not socket_path or not session_token:
            raise RuntimeError("OpenSAC broker environment is not configured")
        return cls(socket_path, session_token)

    def call(self, method: str, params: dict[str, Any]) -> Any:
        client = self._http()
        request = RpcRequest(method=method, params=params)
        try:
            response = client.post("/v1/call", json=request.model_dump())
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise BrokerError(
                f"Broker request failed: {exc}",
                code="broker_transport_error",
                retryable=True,
            ) from exc

        payload = RpcResponse.model_validate(response.json())
        if not payload.ok:
            error = payload.error
            if isinstance(error, RpcError):
                raise BrokerError(
                    error.message,
                    code=error.code,
                    retryable=error.retryable,
                )
            raise BrokerError(
                error or "Broker call failed",
                code="broker_call_failed",
                retryable=False,
            )
        return payload.result

    def _http(self) -> httpx.Client:
        """Return one thread-safe connection pool for this program process."""
        if self._client is not None:
            return self._client
        with self._client_lock:
            if self._client is not None:
                return self._client
            transport = httpx.HTTPTransport(uds=self.socket_path)
            self._client = httpx.Client(
                transport=transport,
                base_url="http://opensac",
                timeout=self.timeout,
                headers=self._headers(),
            )
            return self._client

    def _headers(self) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self.session_token}"}
        execution_id = os.environ.get("OPENSAC_EXECUTION_ID", "").strip()
        if execution_id:
            headers["X-OpenSAC-Execution-ID"] = execution_id
        return headers

    def close(self) -> None:
        with self._client_lock:
            client, self._client = self._client, None
        if client is not None:
            client.close()

    def __enter__(self) -> UnixSocketTransport:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
