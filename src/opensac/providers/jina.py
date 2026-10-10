from __future__ import annotations

import asyncio

import httpx

from ..contracts import Document
from ..errors import ProviderError, ProviderResponseError, ProviderTimeoutError
from ..provider import ProviderConfig, ProviderContext
from .base import ProviderHTTP
from .direct import DirectFetch, is_file_url


class JinaFetch(ProviderHTTP):
    allowed_options = set()

    def __init__(
        self,
        config: ProviderConfig,
        context: ProviderContext,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        direct_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(config, context, transport=transport)
        self._direct = DirectFetch(
            context, transport=direct_transport if direct_transport is not None else transport
        )

    async def aclose(self) -> None:
        try:
            await super().aclose()
        finally:
            await self._direct.aclose()

    async def fetch(self, url: str) -> Document:
        self.validate_config(self.config)
        deadline = asyncio.timeout(self.context.request_timeout)
        primary_error: ProviderError | None = None
        try:
            async with deadline:
                if is_file_url(url):
                    return await self._direct.fetch(url)
                try:
                    async with asyncio.timeout(self.context.request_timeout / 2):
                        return await self._reader_fetch(url)
                except TimeoutError as exc:
                    primary_error = ProviderTimeoutError("Reader request timed out.")
                    primary_error.__cause__ = exc
                except ProviderError as exc:
                    primary_error = exc
                try:
                    return await self._direct.fetch(url)
                except ProviderError as exc:
                    raise exc from primary_error
        except TimeoutError as exc:
            if deadline.expired():
                raise ProviderTimeoutError("Fetch deadline exceeded.") from (primary_error or exc)
            raise

    async def _reader_fetch(self, url: str) -> Document:
        key = self.config.api_key.get_secret_value()
        headers = {"X-Return-Format": "markdown"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        body = await self._request(
            "GET", f"{str(self.config.base_url or 'https://r.jina.ai').rstrip('/')}/{url}", headers
        )
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProviderResponseError("Reader returned invalid text.") from exc
        if not text.strip():
            raise ProviderResponseError("Reader returned empty text.")
        return Document(url=url, text=text)
