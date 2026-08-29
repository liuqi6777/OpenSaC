from __future__ import annotations

import asyncio
import copy
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from ._utils import canonical_json_bytes


@dataclass(slots=True)
class _ProviderCacheEntry:
    value: Any
    size_bytes: int
    expires_at: float


@dataclass(slots=True)
class _ProviderCacheFlight:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


class ProviderResultCache:
    """A bounded process-local cache with per-key miss serialization."""

    def __init__(
        self,
        *,
        ttl_seconds: float,
        max_bytes: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl_seconds < 0:
            raise ValueError("provider result cache TTL cannot be negative")
        if max_bytes < 1:
            raise ValueError("provider result cache max bytes must be at least one")
        self.ttl_seconds = float(ttl_seconds)
        self.max_bytes = int(max_bytes)
        self._clock = clock
        self._entries: OrderedDict[str, _ProviderCacheEntry] = OrderedDict()
        self._flights: dict[str, _ProviderCacheFlight] = {}
        self._lock = asyncio.Lock()
        self.current_bytes = 0
        self.hits = 0
        self.misses = 0
        self.evictions = 0
        self.waiting = 0
        self.coalesced_waiters = 0

    @property
    def enabled(self) -> bool:
        return self.ttl_seconds > 0

    @staticmethod
    def key(namespace: str, provider_identity: str, request_fingerprint: str) -> str:
        return f"{namespace}:{provider_identity}:{request_fingerprint}"

    @staticmethod
    def _encoded_size(value: Any) -> int:
        return len(canonical_json_bytes(value))

    def _remove(self, key: str) -> None:
        entry = self._entries.pop(key, None)
        if entry is None:
            return
        self.current_bytes = max(0, self.current_bytes - entry.size_bytes)
        self.evictions += 1

    def _prune_expired(self, now: float) -> None:
        expired = [key for key, entry in self._entries.items() if entry.expires_at <= now]
        for key in expired:
            self._remove(key)

    async def get(self, key: str, *, record_stats: bool = True) -> tuple[bool, Any]:
        if not self.enabled:
            return False, None
        async with self._lock:
            entry = self._entries.get(key)
            if entry is not None and entry.expires_at <= self._clock():
                self._remove(key)
                entry = None
            if entry is None:
                if record_stats:
                    self.misses += 1
                return False, None
            self._entries.move_to_end(key)
            if record_stats:
                self.hits += 1
            return True, copy.deepcopy(entry.value)

    async def put(self, key: str, value: Any) -> bool:
        if not self.enabled:
            return False
        size_bytes = self._encoded_size(value)
        if size_bytes > self.max_bytes:
            return False
        stored = copy.deepcopy(value)
        async with self._lock:
            self._prune_expired(self._clock())
            self._remove(key)
            self._entries[key] = _ProviderCacheEntry(
                value=stored,
                size_bytes=size_bytes,
                expires_at=self._clock() + self.ttl_seconds,
            )
            self.current_bytes += size_bytes
            while self.current_bytes > self.max_bytes and self._entries:
                oldest = next(iter(self._entries))
                self._remove(oldest)
        return key in self._entries

    @asynccontextmanager
    async def flight(self, key: str) -> AsyncIterator[bool]:
        """Serialize one cache miss key and report whether this caller waited."""

        async with self._lock:
            flight = self._flights.get(key)
            waited = flight is not None
            if flight is None:
                flight = _ProviderCacheFlight()
                await flight.lock.acquire()
                self._flights[key] = flight
            else:
                self.waiting += 1
                self.coalesced_waiters += 1
            flight.users += 1

        if waited:
            acquired = False
            waiting_registered = True
            try:
                await flight.lock.acquire()
                acquired = True
                async with self._lock:
                    self.waiting = max(0, self.waiting - 1)
                    waiting_registered = False
            except BaseException:
                if acquired:
                    flight.lock.release()
                async with self._lock:
                    if waiting_registered:
                        self.waiting = max(0, self.waiting - 1)
                    flight.users -= 1
                    if flight.users == 0:
                        self._flights.pop(key, None)
                raise

        try:
            yield waited
        finally:
            flight.lock.release()
            async with self._lock:
                flight.users -= 1
                if flight.users == 0:
                    self._flights.pop(key, None)

    async def clear(self) -> None:
        async with self._lock:
            self._entries.clear()
            self.current_bytes = 0

    def snapshot(self) -> dict[str, int | float | bool]:
        self._prune_expired(self._clock())
        return {
            "enabled": self.enabled,
            "ttl_seconds": self.ttl_seconds,
            "capacity_bytes": self.max_bytes,
            "current_bytes": self.current_bytes,
            "entries": len(self._entries),
            "hits": self.hits,
            "misses": self.misses,
            "evictions": self.evictions,
            "waiting": self.waiting,
            "coalesced_waiters": self.coalesced_waiters,
            "inflight": len(self._flights),
        }
