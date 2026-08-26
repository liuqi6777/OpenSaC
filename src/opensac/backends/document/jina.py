"""Jina Reader adapter for public web documents."""

from __future__ import annotations

import hashlib
from urllib.parse import urlparse

import httpx

from opensac.backends.document.base import DocumentContent, DocumentHandle
from opensac.backends.document.fallbacks import document_fetch_candidates
from opensac.provider import ProviderRequestError


class JinaReaderBackend:
    name = "web"
    source_kind = "public_url"
    result_cacheable = True
    provider_name = "jina_reader"
    reader_url = "https://r.jina.ai"

    def __init__(
        self,
        api_key: str = "",
        timeout: float = 30.0,
    ) -> None:
        self.api_key = api_key
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    @property
    def provider_identity(self) -> str:
        """Opaque limiter key for the Reader endpoint and credential."""

        material = "\0".join((self.reader_url, self.api_key))
        digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
        return f"jina-reader:{digest}"

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def aclose(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    @staticmethod
    def fetch_candidates(handle: DocumentHandle) -> list[DocumentHandle]:
        return document_fetch_candidates(handle)

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            return {}
        return {"Authorization": f"Bearer {self.api_key}"}

    @staticmethod
    def preflight_fetch(handle: DocumentHandle) -> None:
        """Validate a Reader request before it enters provider governors."""

        parsed_url = urlparse(str(handle.url or ""))
        if parsed_url.scheme.lower() not in {"http", "https"} or not parsed_url.netloc:
            raise ProviderRequestError(
                "invalid_request",
                "Search result cannot be fetched because it has no absolute HTTP URL.",
                retryable=False,
            )

    async def fetch(
        self,
        handle: DocumentHandle,
        *,
        query: str | None = None,
    ) -> DocumentContent:
        del query
        self.preflight_fetch(handle)
        response = await self._http().get(
            f"{self.reader_url}/{handle.url}",
            headers=self._headers(),
        )
        response.raise_for_status()
        text = response.text
        if not isinstance(text, str) or not text.strip():
            raise ProviderRequestError(
                "provider_invalid_response",
                "Reader returned empty document text.",
                retryable=False,
                scope="resource",
            )
        return DocumentContent(
            source=handle.source,
            text=text,
            url=handle.url,
            title=handle.title,
            metadata={"backend": self.name},
        )
