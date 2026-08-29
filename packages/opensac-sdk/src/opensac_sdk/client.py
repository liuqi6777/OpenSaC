from __future__ import annotations

import threading

from ._resources import (
    CapabilitiesResource,
    ContentResource,
    LLMResource,
    SearchResource,
    WorkspaceResource,
)
from .transport import UnixSocketTransport


class OpenSACClient:
    """Constructed SDK client that owns broker transport and resource namespaces."""

    def __init__(
        self,
        transport: UnixSocketTransport,
        workspace: WorkspaceResource,
    ) -> None:
        self._transport = transport
        self.search = SearchResource(transport)
        self.content = ContentResource(transport)
        self.capabilities = CapabilitiesResource(transport)
        self.llm = LLMResource(transport)
        self.workspace = workspace

    @classmethod
    def from_environment(cls) -> OpenSACClient:
        transport = UnixSocketTransport.from_environment()
        return cls(
            transport,
            WorkspaceResource.from_environment(),
        )

    def close(self) -> None:
        self._transport.close()

    def __enter__(self) -> OpenSACClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class LazyOpenSACClient:
    """OpenSAC sandbox entry point, available as ``opensac_sdk.sdk``.

    Namespaces are ``search``, ``content``, ``llm``, and ``workspace``;
    ``capabilities()`` inspects the active deployment. Print bounded results for
    the calling agent. Reading one namespace or method ``.__doc__`` does not call
    the broker.
    """

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
