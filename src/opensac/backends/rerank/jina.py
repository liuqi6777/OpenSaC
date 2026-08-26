from __future__ import annotations

import hashlib
import math

import httpx

from opensac.backends._response import json_object
from opensac.backends.rerank.base import RerankScore
from opensac.provider import ProviderRequestError, invalid_provider_response


class JinaReranker:
    """Strict adapter for Jina's index-addressed text reranker response."""

    endpoint = "https://api.jina.ai/v1/rerank"
    provider_name = "jina_reranker"

    def __init__(
        self,
        *,
        api_key: str = "",
        model: str = "",
        timeout: float = 30.0,
    ) -> None:
        self.api_key = api_key.strip()
        self.model = model.strip()
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    @property
    def name(self) -> str:
        return f"jina:{self.model}" if self.model else "jina"

    @property
    def provider_identity(self) -> str:
        material = "\0".join((self.endpoint, self.api_key, self.model))
        digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
        return f"jina-reranker:{digest}"

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def aclose(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    def preflight(self) -> None:
        missing = []
        if not self.api_key:
            missing.append("API key")
        if not self.model:
            missing.append("model")
        if missing:
            verb = "is" if len(missing) == 1 else "are"
            raise ProviderRequestError(
                "provider_not_configured",
                f"Reranker {' and '.join(missing)} {verb} not configured.",
                retryable=False,
            )

    async def rerank(
        self,
        query: str,
        documents: list[str],
    ) -> list[RerankScore]:
        self.preflight()
        if not documents:
            return []
        response = await self._http().post(
            self.endpoint,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.model,
                "query": query,
                "documents": documents,
                "top_n": len(documents),
                "return_documents": False,
            },
        )
        response.raise_for_status()
        payload = json_object(response)
        rows = payload.get("results")
        if not isinstance(rows, list) or len(rows) != len(documents):
            raise invalid_provider_response()

        results: list[RerankScore] = []
        indexes: set[int] = set()
        for row in rows:
            if not isinstance(row, dict):
                raise invalid_provider_response()
            index = row.get("index")
            score = row.get("relevance_score")
            if (
                isinstance(index, bool)
                or not isinstance(index, int)
                or index < 0
                or index >= len(documents)
                or index in indexes
                or isinstance(score, bool)
                or not isinstance(score, (int, float))
                or not math.isfinite(float(score))
            ):
                raise invalid_provider_response()
            indexes.add(index)
            results.append(RerankScore(index=index, score=float(score)))
        if indexes != set(range(len(documents))):
            raise invalid_provider_response()
        return results
