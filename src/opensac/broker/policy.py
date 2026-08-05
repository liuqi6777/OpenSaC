from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from opensac.models import RunLimits, RunUsage


class QuotaExceeded(RuntimeError):
    pass


@dataclass
class CapabilityPolicy:
    limits: RunLimits
    allowed_backends: set[str]
    usage: RunUsage = field(default_factory=RunUsage)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def consume_search(self, amount: int = 1) -> None:
        async with self._lock:
            if self.usage.search_calls + amount > self.limits.max_search_calls:
                raise QuotaExceeded("Search call quota exceeded")
            self.usage.search_calls += amount

    async def consume_llm(self, amount: int = 1) -> None:
        async with self._lock:
            if self.usage.llm_calls + amount > self.limits.max_llm_calls:
                raise QuotaExceeded("LLM call quota exceeded")
            self.usage.llm_calls += amount

    def require_backend(self, backend: str) -> None:
        if backend not in self.allowed_backends:
            raise PermissionError(f"Backend '{backend}' is not enabled for this session")
