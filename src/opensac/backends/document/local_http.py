"""HTTP adapter for documents stored by the local retrieval service."""

from __future__ import annotations

import hashlib
import re
from urllib.parse import urljoin

import httpx

from opensac.backends._response import json_object
from opensac.backends.document.base import DocumentContent, DocumentHandle
from opensac.provider import ProviderRequestError, invalid_provider_response

# Full documents in the local corpus carry a small YAML-like frontmatter block.
# Deliberately avoid a YAML dependency: malformed lines are ignored instead of
# turning a readable document into a provider failure.
_FRONTMATTER_PATTERN = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*\n?", re.DOTALL)


def parse_document_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Split frontmatter fields from a local document body when present."""

    match = _FRONTMATTER_PATTERN.match(text)
    if match is None:
        return {}, text
    fields: dict[str, str] = {}
    for line in match.group(1).splitlines():
        key, separator, value = line.partition(":")
        key = key.strip().lower()
        if separator and key and key not in fields:
            fields[key] = value.strip()
    return fields, text[match.end() :]


class LocalDocumentBackend:
    name = "local"
    source_kind = "opaque"
    result_cacheable = False
    provider_name = "local_search"

    def __init__(
        self,
        base_url: str,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    @property
    def provider_identity(self) -> str:
        """Opaque limiter key that changes with the effective endpoint."""

        digest = hashlib.sha256(self.base_url.encode("utf-8")).hexdigest()
        return f"local-document:{digest}"

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
        return [handle]

    async def fetch(
        self,
        handle: DocumentHandle,
        *,
        query: str | None = None,
    ) -> DocumentContent:
        del query
        self.preflight_fetch(handle)
        response = await self._http().post(
            urljoin(self.base_url, "get_document"),
            json={"docid": handle.docid},
        )
        response.raise_for_status()
        payload = json_object(response)
        raw_text = payload.get("text")
        if not isinstance(raw_text, str):
            raise invalid_provider_response()
        fields, _ = parse_document_frontmatter(raw_text)
        metadata: dict[str, object] = {"backend": self.name}
        if date := handle.date or fields.get("date"):
            metadata["date"] = date
        return DocumentContent(
            source=handle.source,
            # Preserve the header so grep/read line coordinates stay stable.
            text=raw_text,
            title=handle.title or fields.get("title", ""),
            metadata=metadata,
        )

    @staticmethod
    def preflight_fetch(handle: DocumentHandle) -> None:
        """Validate a local document handle before provider admission."""

        if not handle.docid:
            raise ProviderRequestError(
                "invalid_request",
                "Local search result has no document identifier.",
                retryable=False,
            )
