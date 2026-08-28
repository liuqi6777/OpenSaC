from __future__ import annotations

import os
import threading
from typing import Any

import httpx

from ._record import wrap
from ._version import CAPABILITY_CONTRACT


class BrokerError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str,
        retryable: bool,
        attempts: int | None = None,
        provider_status: int | None = None,
        retry_after_seconds: float | None = None,
        provider: str | None = None,
        component: str | None = None,
        scope: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.attempts = attempts
        self.provider_status = provider_status
        self.retry_after_seconds = retry_after_seconds
        self.provider = provider
        self.component = component
        self.scope = scope


class UnixSocketTransport:
    def __init__(
        self,
        socket_path: str,
        session_token: str | None,
        timeout: float | None = None,
        *,
        environment_context: bool = False,
    ) -> None:
        self.socket_path = socket_path
        self.session_token = session_token
        self.timeout = timeout
        self.environment_context = environment_context
        self._client: httpx.Client | None = None
        self._client_lock = threading.Lock()

    @classmethod
    def from_environment(cls) -> UnixSocketTransport:
        socket_path = os.environ.get("OPENSAC_BROKER_SOCKET")
        session_token = os.environ.get("OPENSAC_SESSION_TOKEN")
        if not socket_path or not session_token:
            raise RuntimeError("OpenSAC broker environment is not configured")
        return cls(socket_path, None, environment_context=True)

    def call(self, method: str, params: dict[str, Any]) -> Any:
        client = self._http()
        try:
            response = client.post(
                "/v1/call",
                json={"method": method, "params": params},
                headers=self._headers(),
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise BrokerError(
                f"Broker request failed: {exc}",
                code="broker_transport_error",
                retryable=True,
            ) from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise BrokerError(
                "Broker returned invalid JSON",
                code="broker_protocol_error",
                retryable=False,
            ) from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("ok"), bool):
            raise BrokerError(
                "Broker returned an invalid response envelope",
                code="broker_protocol_error",
                retryable=False,
            )
        reported_contract = payload.get("capability_contract")
        if isinstance(reported_contract, bool) or reported_contract != CAPABILITY_CONTRACT:
            reported = "missing" if reported_contract is None else repr(reported_contract)
            raise BrokerError(
                f"Capability contract mismatch: SDK requires {CAPABILITY_CONTRACT}, "
                f"broker reported {reported}. Deploy matching OpenSAC SDK, broker, "
                "and sandbox versions.",
                code="capability_contract_mismatch",
                retryable=False,
            )
        if not payload["ok"]:
            error = payload.get("error")
            if isinstance(error, dict):
                raise BrokerError(
                    str(error.get("message") or "Broker call failed"),
                    code=str(error.get("code") or "broker_call_failed"),
                    retryable=bool(error.get("retryable", False)),
                    attempts=error.get("attempts"),
                    provider_status=error.get("provider_status"),
                    retry_after_seconds=error.get("retry_after_seconds"),
                    provider=error.get("provider"),
                    component=error.get("component"),
                    scope=error.get("scope"),
                )
            raise BrokerError(
                str(error or "Broker call failed"),
                code="broker_call_failed",
                retryable=False,
            )
        return wrap(payload.get("result"))

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
            )
            return self._client

    def _headers(self) -> dict[str, str]:
        session_token = (
            os.environ.get("OPENSAC_SESSION_TOKEN")
            if self.environment_context
            else self.session_token
        )
        if not session_token:
            raise RuntimeError("OpenSAC broker environment is not configured")
        headers = {
            "Authorization": f"Bearer {session_token}",
            "X-OpenSAC-Capability-Contract": str(CAPABILITY_CONTRACT),
        }
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
