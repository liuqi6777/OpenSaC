from collections.abc import Iterator

import pytest

from opensac import Client
from opensac.contracts import Document, SearchHit
from opensac.errors import CapabilityError


class FakeProvider:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.closed = False

    async def search(self, query: str, limit: int) -> list[SearchHit]:
        self.calls.append(query)
        if query == "empty":
            return []
        return [SearchHit(url="https://example.com/paper", title=query, snippet="Evidence")][:limit]

    async def fetch(self, url: str) -> Document:
        self.calls.append(url)
        if url.endswith("/failed"):
            raise CapabilityError("provider_timeout", "Reader timed out.", 504, True)
        return Document(url=url, text="Full document")

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def provider() -> FakeProvider:
    return FakeProvider()


@pytest.fixture
def sdk_client(provider: FakeProvider, monkeypatch) -> Iterator[Client]:
    from types import SimpleNamespace

    from opensac import provider as registry

    monkeypatch.setattr(
        registry,
        "entry_points",
        lambda **kwargs: [SimpleNamespace(load=lambda: lambda config, context: provider)],
    )
    with Client() as client:
        yield client
