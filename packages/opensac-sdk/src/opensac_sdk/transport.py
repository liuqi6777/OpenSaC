from __future__ import annotations

import os
from typing import Any

import httpx

from .models import RpcRequest, RpcResponse


class BrokerError(RuntimeError):
    pass


class UnixSocketTransport:
    def __init__(self, socket_path: str, session_token: str, timeout: float = 60.0) -> None:
        self.socket_path = socket_path
        self.session_token = session_token
        self.timeout = timeout

    @classmethod
    def from_environment(cls) -> UnixSocketTransport:
        socket_path = os.environ.get("OPENSAC_BROKER_SOCKET")
        session_token = os.environ.get("OPENSAC_SESSION_TOKEN")
        if not socket_path or not session_token:
            raise RuntimeError("OpenSAC broker environment is not configured")
        return cls(socket_path, session_token)

    def call(self, method: str, params: dict[str, Any]) -> Any:
        transport = httpx.HTTPTransport(uds=self.socket_path)
        headers = {"Authorization": f"Bearer {self.session_token}"}
        request = RpcRequest(method=method, params=params)
        try:
            with httpx.Client(
                transport=transport,
                base_url="http://opensac",
                timeout=self.timeout,
                headers=headers,
            ) as client:
                response = client.post("/v1/call", json=request.model_dump())
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise BrokerError(f"Broker request failed: {exc}") from exc

        payload = RpcResponse.model_validate(response.json())
        if not payload.ok:
            raise BrokerError(payload.error or "Broker call failed")
        return payload.result
