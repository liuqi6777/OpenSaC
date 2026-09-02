from __future__ import annotations

import asyncio
import hashlib
import socket
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import uvicorn

from opensac.broker.app import create_broker_app
from opensac.broker.service import BrokerService


class BrokerAlreadyRunning(RuntimeError):
    pass


def resolve_broker_socket_path(path: Path) -> Path:
    """Return a bindable AF_UNIX path, shortening long macOS temp paths.

    Darwin's ``sockaddr_un.sun_path`` is only 104 bytes. Pytest and deeply
    nested workspaces routinely exceed that even though the configured path is
    otherwise valid. Both the broker and Docker sandbox use the returned path,
    so no symlink or client-side special case is required.
    """
    resolved = path.resolve()
    if len(str(resolved).encode()) <= 100:
        return resolved
    digest = hashlib.sha256(str(resolved).encode()).hexdigest()[:20]
    return Path("/tmp") / f"opensac-{digest}" / "broker.sock"


def _is_live_socket(path: Path) -> bool:
    """Is a process currently accepting connections on this Unix socket?

    A socket file left behind by a crashed process is indistinguishable from a
    live one by `stat` alone; only a connect attempt tells them apart.
    """
    if not path.exists():
        return False
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(0.5)
    try:
        probe.connect(str(path))
    except OSError:
        return False
    else:
        return True
    finally:
        probe.close()


class _EmbeddedServer(uvicorn.Server):
    """A uvicorn server that keeps its hands off process-wide state.

    The broker runs inside the public API process, so the outer server must stay
    in charge of SIGINT/SIGTERM. Without this override the broker would install
    its own handlers and swallow the shutdown signal meant for the API server.
    """

    @contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


class BrokerRuntime:
    def __init__(self, service: BrokerService, socket_path: Path) -> None:
        self.service = service
        self.configured_socket_path = socket_path.resolve()
        self.socket_path = resolve_broker_socket_path(socket_path)
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task[None] | None = None
        self._owns_socket = False

    async def start(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        # Refuse to evict a broker that is still serving. Unlinking it would
        # not stop that process: it keeps its bound socket and answers the
        # health check, while every sandbox launched afterwards fails to bind
        # a mount source that no longer exists. The symptom (containers exit
        # 125) points nowhere near the cause, so fail here instead. A second
        # instance is never what the operator wants anyway, since both would
        # also share one data directory.
        if _is_live_socket(self.socket_path):
            raise BrokerAlreadyRunning(
                f"Another OpenSAC broker is already listening on {self.socket_path}. "
                "Stop it before starting a new one, or point this instance at a "
                "different OPENSAC_BROKER_SOCKET and OPENSAC_DATA_DIR."
            )
        # Anything left here now is a stale file from a process that died
        # without cleaning up; bind() would fail with EADDRINUSE against it.
        self.socket_path.unlink(missing_ok=True)
        config = uvicorn.Config(
            create_broker_app(self.service),
            uds=str(self.socket_path),
            # `log_config`/`log_level`/`access_log` are process-wide in uvicorn:
            # Config.__init__ calls configure_logging(), which would reset logging
            # and silence the public API server's own logs.
            log_config=None,
        )
        self._server = _EmbeddedServer(config)
        self._task = asyncio.create_task(self._server.serve())
        for _ in range(100):
            if self.socket_path.exists():
                self._owns_socket = True
                return
            if self._task.done():
                await self._task
            await asyncio.sleep(0.02)
        raise RuntimeError("Capability broker did not create its Unix socket")

    async def stop(self) -> None:
        try:
            if self._server is not None:
                self._server.should_exit = True
            if self._task is not None:
                await self._task
        finally:
            # Only clean up a socket this runtime actually created. A runtime
            # that bailed out because someone else owned the path must not
            # delete it on the way down -- that is the same eviction, deferred.
            if self._owns_socket:
                self.socket_path.unlink(missing_ok=True)
                self._owns_socket = False
            await self.service.aclose()
