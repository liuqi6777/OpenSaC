from __future__ import annotations

from ..contracts import Document
from ..errors import ProviderResponseError
from .base import ProviderHTTP


class JinaFetch(ProviderHTTP):
    async def fetch(self, url: str) -> Document:
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
