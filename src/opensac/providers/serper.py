from __future__ import annotations

import json

from ..contracts import SearchHit
from ..errors import CapabilityError, NotConfiguredError, ProviderResponseError
from .base import ProviderHTTP


class SerperSearch(ProviderHTTP):
    async def search(self, query: str, limit: int) -> list[SearchHit]:
        key = self.config.api_key.get_secret_value()
        if not key:
            raise NotConfiguredError("Search provider is not configured.")
        body = await self._request(
            "POST",
            str(self.config.base_url or "https://google.serper.dev/search"),
            {"X-API-KEY": key},
            {"q": query, "num": limit},
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
        except (ValueError, KeyError, TypeError, AttributeError, CapabilityError) as exc:
            raise ProviderResponseError("Search provider returned invalid data.") from exc
