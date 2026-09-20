from __future__ import annotations

import json
from typing import Any, ClassVar

import httpx
from pydantic import SecretStr

from ..errors import (
    ConfigurationError,
    NotConfiguredError,
    ProviderError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    ResponseTooLargeError,
)
from ..provider import ProviderConfig, ProviderContext


class ProviderHTTP:
    requires_base_url = False
    reserved_options: ClassVar[set[str]] = set()
    allowed_options: ClassVar[set[str] | None] = None

    @classmethod
    def validate_options(cls, config: ProviderConfig) -> None:
        if cls.reserved_options.intersection(config.options):
            raise ConfigurationError("Provider options override SDK-managed fields.")
        if cls.allowed_options is not None and config.options.keys() - cls.allowed_options:
            raise ConfigurationError("Provider options contain unsupported fields.")
        try:
            json.dumps(config.options, allow_nan=False)
        except (ValueError, TypeError, RecursionError) as exc:
            raise ConfigurationError("Provider options must contain finite JSON data.") from exc

    @classmethod
    def validate_config(cls, config: ProviderConfig) -> None:
        cls.validate_options(config)
        if cls.requires_base_url and config.base_url is None:
            raise NotConfiguredError("Provider endpoint is required.")

    def __init__(
        self,
        config: ProviderConfig,
        context: ProviderContext,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config = config.model_copy(deep=True)
        self.validate_options(self.config)
        self.context = context
        if self.requires_base_url and config.base_url is None:
            raise ValueError("This provider requires base_url")
        self._http = httpx.AsyncClient(
            timeout=context.request_timeout,
            follow_redirects=False,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(
        self, method: str, url: str, headers: dict[str, str], body: dict[str, Any] | None = None
    ) -> bytes:
        try:
            async with self._http.stream(method, url, headers=headers, json=body) as response:
                if response.status_code == 429:
                    raise ProviderRateLimitError("Provider rate limited.")
                if not response.is_success:
                    raise ProviderError(
                        "Provider request failed.", retryable=response.status_code >= 500
                    )
                chunks = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    if len(chunks) + len(chunk) > self.context.max_response_bytes:
                        raise ResponseTooLargeError("Provider response exceeds limit.")
                    chunks.extend(chunk)
                return bytes(chunks)
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError("Provider request timed out.") from exc
        except httpx.DecodingError as exc:
            raise ProviderResponseError("Provider returned invalid encoded data.") from exc
        except httpx.TransportError as exc:
            raise ProviderUnavailableError("Provider connection failed.") from exc

    async def request_json(
        self, base_url: str, path: str, key: SecretStr, payload: dict[str, Any]
    ) -> Any:
        headers = (
            {"Authorization": f"Bearer {key.get_secret_value()}"} if key.get_secret_value() else {}
        )
        body = await self._request("POST", f"{base_url.rstrip('/')}/{path}", headers, payload)
        try:
            return json.loads(body)
        except (ValueError, RecursionError) as exc:
            raise ProviderResponseError("Provider returned invalid JSON.") from exc
