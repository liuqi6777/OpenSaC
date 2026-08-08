from __future__ import annotations

from .citations import CitationsResource
from .content import ContentResource
from .llm import LLMResource
from .output import OutputResource
from .search import SearchResource
from .state import StateResource
from .transport import UnixSocketTransport


class OpenSACClient:
    def __init__(
        self,
        transport: UnixSocketTransport,
        state: StateResource,
        output: OutputResource,
    ) -> None:
        self.search = SearchResource(transport)
        self.content = ContentResource(transport)
        self.citations = CitationsResource(transport)
        self.llm = LLMResource(transport)
        self.state = state
        self.output = output

    @classmethod
    def from_environment(cls) -> OpenSACClient:
        transport = UnixSocketTransport.from_environment()
        return cls(
            transport,
            StateResource.from_environment(),
            OutputResource.from_environment(transport),
        )


class LazyOpenSACClient:
    def __init__(self) -> None:
        self._client: OpenSACClient | None = None

    def _get(self) -> OpenSACClient:
        if self._client is None:
            self._client = OpenSACClient.from_environment()
        return self._client

    def __getattr__(self, name: str):
        return getattr(self._get(), name)
