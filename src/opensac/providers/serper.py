from __future__ import annotations

import json

from ..contracts import SearchHit
from ..errors import CapabilityError, ConfigurationError, NotConfiguredError, ProviderResponseError
from ..provider import ProviderConfig
from .base import ProviderHTTP


class SerperSearch(ProviderHTTP):
    allowed_options = {"gl", "hl"}
    reserved_options = {"q", "num"}

    @classmethod
    def validate_options(cls, config: ProviderConfig) -> None:
        super().validate_options(config)
        if any(
            not isinstance(value, str) or not value.strip() for value in config.options.values()
        ):
            raise ConfigurationError("Serper gl and hl options must be nonempty strings.")

    @classmethod
    def validate_config(cls, config: ProviderConfig) -> None:
        super().validate_config(config)
        if not config.api_key.get_secret_value():
            raise NotConfiguredError("Search provider API key is required.")

    async def search(self, query: str, limit: int) -> list[SearchHit]:
        self.validate_config(self.config)
        key = self.config.api_key.get_secret_value()
        body = await self._request(
            "POST",
            str(self.config.base_url or "https://google.serper.dev/search"),
            {"X-API-KEY": key},
            {**self.config.options, "q": query, "num": limit},
        )
        try:
            payload = json.loads(body)
            organic = payload["organic"]
            if not isinstance(organic, list):
                raise ValueError("Expected list")
            hits = []
            for item in organic[:limit]:
                url = item["link"]
                hits.append(
                    SearchHit(
                        url=url,
                        title=item.get("title", ""),
                        snippet=item.get("snippet", ""),
                        domain=item.get("domain"),
                        date=item.get("date"),
                    )
                )
            return hits
        except (
            ValueError,
            KeyError,
            TypeError,
            AttributeError,
            RecursionError,
            CapabilityError,
        ) as exc:
            raise ProviderResponseError("Search provider returned invalid data.") from exc
