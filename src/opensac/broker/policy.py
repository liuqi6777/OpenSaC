from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from opensac.models import RunUsage


class MechanismDisabled(RuntimeError):
    """A capability the session's experimental arm has switched off.

    Deliberately not a PermissionError: the broker maps those to HTTP 403,
    whose body the SDK transport discards, so the program would see only
    "Broker request failed". This one travels the ordinary error path, which
    preserves the message -- and the message is the whole point, since the
    program has to restructure itself around the missing capability.
    """


@dataclass
class CapabilityPolicy:
    """What a session is allowed to reach, and what it has spent.

    Two jobs that look similar and are not. `require_backend` is a boundary:
    refusing it is the point, and a session that reaches past it is a bug.
    Everything else here only counts.

    There is deliberately no ceiling on the counts. A hard cap in a research
    harness has two possible fates and neither is useful: set high enough not to
    interfere it is dead code, and set low enough to bind it converts a question
    into a zero that reads afterwards as a model failure rather than as the
    budget it was. The numbers are instead handed to the program through
    `session.usage`, so a policy that wants to ration retrieval can be written
    and measured rather than imposed.
    """

    allowed_backends: set[str]
    usage: RunUsage = field(default_factory=RunUsage)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def record_search(self, amount: int = 1) -> None:
        async with self._lock:
            self.usage.search_calls += amount

    async def record_llm(self, amount: int = 1) -> None:
        async with self._lock:
            self.usage.llm_calls += amount

    async def record_content_fetches(self, requested: int, from_backend: int) -> None:
        """Charge one ``content.*`` call against both fetch counters.

        Taken together they say what a cache saved. ``requested`` follows the
        program's behaviour and ``from_backend`` follows the bill, and the two
        stop being the same number the moment a document is read twice.
        """
        async with self._lock:
            self.usage.content_fetches += requested
            self.usage.content_backend_fetches += from_backend

    async def record_pipeline_model_tokens(self, amount: int) -> None:
        async with self._lock:
            self.usage.pipeline_model_tokens += amount

    async def record_sandbox_seconds(self, amount: float) -> None:
        async with self._lock:
            self.usage.sandbox_seconds += amount

    def require_backend(self, backend: str) -> None:
        if backend not in self.allowed_backends:
            raise PermissionError(f"Backend '{backend}' is not enabled for this session")
