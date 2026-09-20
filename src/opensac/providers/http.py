"""Adapters for configurable HTTP capability endpoints."""

from pydantic import TypeAdapter

from ..contracts import Document, RerankResult, SearchHit
from ..errors import NotConfiguredError, ProviderResponseError
from ..provider import ProviderConfig
from .base import ProviderHTTP


class HTTPSearch(ProviderHTTP):
    """Adapt a search capability endpoint returning URL-based SearchHit objects."""

    requires_base_url = True
    reserved_options = {"query", "limit"}

    async def search(self, query: str, limit: int) -> list[SearchHit]:
        self.validate_config(self.config)
        payload = await self.request_json(
            str(self.config.base_url),
            "search",
            self.config.api_key,
            {**self.config.options, "query": query, "limit": limit},
        )
        try:
            return TypeAdapter(list[SearchHit]).validate_python(payload["data"])[:limit]
        except (ValueError, KeyError, TypeError) as exc:
            raise ProviderResponseError("Search provider returned invalid data.") from exc


class HTTPFetch(ProviderHTTP):
    """Adapt a fetch capability endpoint; URL resolution belongs to the endpoint."""

    requires_base_url = True
    reserved_options = {"url"}

    async def fetch(self, url: str) -> Document:
        self.validate_config(self.config)
        payload = await self.request_json(
            str(self.config.base_url),
            "fetch",
            self.config.api_key,
            {**self.config.options, "url": url},
        )
        try:
            document = Document.model_validate(payload["data"])
            if document.url != url:
                raise ValueError("Provider changed the requested URL")
            return document
        except (ValueError, KeyError, TypeError) as exc:
            raise ProviderResponseError("Fetch provider returned invalid data.") from exc


class HTTPRerank(ProviderHTTP):
    reserved_options = {"model", "query", "documents", "top_n"}

    @classmethod
    def validate_config(cls, config: ProviderConfig) -> None:
        super().validate_config(config)
        if config.base_url is None or not config.model:
            raise NotConfiguredError("Rerank endpoint and model are required.")

    async def rerank(
        self,
        query: str,
        documents: list[str],
        *,
        top_n: int | None = None,
    ) -> list[RerankResult]:
        model = self.config.model
        self.validate_config(self.config)
        payload = await self.request_json(
            str(self.config.base_url),
            "rerank",
            self.config.api_key,
            {
                **self.config.options,
                "query": query,
                "documents": documents,
                "top_n": top_n or len(documents),
                "model": model,
            },
        )
        try:
            return TypeAdapter(list[RerankResult]).validate_python(payload["results"])
        except (ValueError, KeyError, TypeError) as exc:
            raise ProviderResponseError("Rerank provider returned invalid data.") from exc
