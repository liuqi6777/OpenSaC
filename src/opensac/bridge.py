"""Synchronous access to the local async runtime."""

from __future__ import annotations

import threading
from collections.abc import Awaitable, Callable
from contextlib import AbstractContextManager
from typing import Any, overload

from anyio.from_thread import BlockingPortal, start_blocking_portal
from pydantic import ValidationError

from opensac.config import Settings
from opensac.runtime import Runtime

from .errors import ClientClosedError, ConfigurationError


class SyncRuntime:
    """Keep async provider resources on one persistent event loop across sync calls.

    No HTTP server or subprocesses. Providers and bounded schema validation stay in
    this process. Also works in an active event loop.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings
        self._manager: AbstractContextManager[BlockingPortal] | None = None
        self._portal: BlockingPortal | None = None
        self._runtime: Runtime | None = None
        self._lock = threading.Lock()
        self._closed = False

    def _get(self) -> tuple[BlockingPortal, Runtime]:
        with self._lock:
            if self._closed:
                raise ClientClosedError("The client is closed.")
            if self._portal is None or self._runtime is None:
                manager = start_blocking_portal(name="opensac-runtime")
                portal = manager.__enter__()
                try:
                    runtime = portal.call(lambda: Runtime(self._settings))
                except ValidationError as exc:
                    manager.__exit__(None, None, None)
                    raise ConfigurationError("Invalid provider configuration.") from exc
                except BaseException:
                    manager.__exit__(None, None, None)
                    raise
                self._manager, self._portal, self._runtime = manager, portal, runtime
            return self._portal, self._runtime

    @overload
    def call[T](self, operation: Callable[[Runtime], Awaitable[T]]) -> T: ...

    @overload
    def call[T](self, operation: Callable[[Runtime], T]) -> T: ...

    def call(self, operation: Callable[[Runtime], Any]) -> Any:
        portal, runtime = self._get()
        return portal.call(operation, runtime)

    def close(self) -> None:
        # Close after active requests complete.
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                if self._portal is not None and self._runtime is not None:
                    self._portal.call(self._runtime.aclose)
            finally:
                if self._manager is not None:
                    self._manager.__exit__(None, None, None)
                self._manager = self._portal = self._runtime = None
