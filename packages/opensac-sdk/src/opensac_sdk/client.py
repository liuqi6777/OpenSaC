from __future__ import annotations

import threading

from .citations import CitationsResource
from .content import ContentResource
from .llm import LLMResource
from .output import OutputResource
from .search import SearchResource
from .session import SessionResource
from .state import StateResource
from .transport import UnixSocketTransport


class OpenSACClient:
    def __init__(
        self,
        transport: UnixSocketTransport,
        state: StateResource,
        output: OutputResource,
    ) -> None:
        self._transport = transport
        self.search = SearchResource(transport)
        self.content = ContentResource(transport)
        self.citations = CitationsResource(transport)
        self.session = SessionResource(transport)
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

    def close(self) -> None:
        self._transport.close()

    def __enter__(self) -> OpenSACClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class LazyOpenSACClient:
    def __init__(self) -> None:
        self._client: OpenSACClient | None = None
        self._client_lock = threading.Lock()

    def _get(self) -> OpenSACClient:
        if self._client is not None:
            return self._client
        with self._client_lock:
            if self._client is None:
                self._client = OpenSACClient.from_environment()
            return self._client

    def close(self) -> None:
        with self._client_lock:
            client, self._client = self._client, None
        if client is not None:
            client.close()

    def __getattr__(self, name: str):
        return getattr(self._get(), name)
