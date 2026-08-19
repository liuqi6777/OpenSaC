"""Adapters for Serper search and its currently coupled Jina reader."""

from __future__ import annotations

import hashlib
from typing import Any
from urllib.parse import urlparse

import httpx

from opensac._contracts import ContentSnippet, RetrievalMetadata, SearchHit
from opensac.provider import ProviderRequestError, invalid_provider_response


class SerperBackend:
    name = "web"
    search_url = "https://google.serper.dev/search"
    reader_url = "https://r.jina.ai"
    # Pushed down into the query as `site:` rather than filtered afterwards,
    # which is the point: the ranking is recomputed under the constraint instead
    # of being thinned after the fact.
    supports_domains = True
    # Results one SERP request will serve. Depth past this is not a matter of
    # asking harder -- there is no such response -- so it is refused rather
    # than quietly clipped. A program told it read rank 150 when it read rank
    # 100 draws exactly the wrong conclusion about why it found nothing.
    max_depth = 100

    def __init__(
        self,
        api_key: str = "",
        timeout: float = 30.0,
        fetch_concurrency: int = 6,
        jina_api_key: str = "",
    ) -> None:
        self.api_key = api_key
        self.jina_api_key = jina_api_key
        self.timeout = timeout
        # Kept while callers migrate concurrency ownership to ProviderRuntime.
        # The adapter itself performs one transport operation per method.
        del fetch_concurrency
        self._client: httpx.AsyncClient | None = None

    @property
    def provider_identity(self) -> str:
        """Opaque limiter key for the endpoint and configured credential."""

        material = "\0".join((self.search_url, self.reader_url, self.api_key, self.jina_api_key))
        digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
        return f"web:{digest}"

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def aclose(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            raise ProviderRequestError(
                "provider_not_configured",
                "Web provider credentials are not configured.",
                retryable=False,
            )
        return {"X-API-KEY": self.api_key, "Content-Type": "application/json"}

    def preflight_search(self) -> None:
        """Validate deployment-owned search configuration before admission."""

        self._headers()

    def preflight_fetch(self, hit: SearchHit) -> None:
        """Validate a Reader request before it enters provider governors."""

        parsed_url = urlparse(str(hit.url or ""))
        if parsed_url.scheme.lower() not in {"http", "https"} or not parsed_url.netloc:
            raise ProviderRequestError(
                "invalid_request",
                "Search result cannot be fetched because it has no absolute HTTP URL.",
                retryable=False,
            )

    def _reader_headers(self) -> dict[str, str]:
        if not self.jina_api_key:
            return {}
        return {"Authorization": f"Bearer {self.jina_api_key}"}

    async def search(
        self,
        query: str,
        *,
        limit: int,
        offset: int = 0,
        domains: list[str] | None = None,
    ) -> list[SearchHit]:
        self.preflight_search()
        if domains:
            sites = " OR ".join(f"site:{domain}" for domain in domains)
            query = f"{query} ({sites})"
        # Deepen the request and slice, rather than using Serper's `page`:
        # paging renumbers `position` per page, and the rank a hit carries has
        # to stay comparable across the two backends and joinable offline.
        # `max_depth` is declared, not checked here: the broker refuses a
        # request past it before this runs, so that every backend's ceiling is
        # reported to the program in the same words.
        depth = offset + limit
        response = await self._http().post(
            self.search_url,
            headers=self._headers(),
            json={"q": query, "num": depth},
        )
        response.raise_for_status()
        payload = self._json_object(response)
        organic = payload.get("organic", [])
        if not isinstance(organic, list) or not all(isinstance(hit, dict) for hit in organic):
            raise invalid_provider_response()
        try:
            return [
                self._normalize_hit(hit, index + 1)
                for index, hit in enumerate(organic[:depth])
                if index >= offset
            ]
        except (TypeError, ValueError) as exc:
            raise invalid_provider_response() from exc

    @staticmethod
    def _json_object(response: Any) -> dict[str, Any]:
        try:
            payload = response.json()
        except Exception as exc:
            raise invalid_provider_response() from exc
        if not isinstance(payload, dict):
            raise invalid_provider_response()
        return payload

    def _normalize_hit(self, hit: dict, rank: int) -> SearchHit:
        url = str(hit.get("link", "") or "")
        return SearchHit(
            backend=self.name,
            title=str(hit.get("title", "") or ""),
            url=url,
            domain=urlparse(url).netloc or None,
            snippet=str(hit.get("snippet", "") or ""),
            rank=int(hit.get("position", rank)),
            retrieval=RetrievalMetadata(
                mode="organic",
                result_mode="snippet",
                comparable_across_queries=False,
            ),
            metadata={k: v for k, v in hit.items() if k not in {"title", "link", "snippet"}},
        )

    async def fetch(
        self,
        hit: SearchHit,
        *,
        query: str | None = None,
    ) -> ContentSnippet:
        del query
        self.preflight_fetch(hit)
        response = await self._http().get(
            f"{self.reader_url}/{hit.url}",
            headers=self._reader_headers(),
        )
        response.raise_for_status()
        text = response.text
        if not isinstance(text, str):
            raise invalid_provider_response()
        return ContentSnippet(
            source=hit.source,
            text=text,
            url=hit.url,
            title=hit.title,
            metadata={"backend": self.name},
        )
