from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from opensac.models import ResourceBudget, RunUsage, budget_remaining


class BudgetExceeded(RuntimeError):
    """A non-retryable session resource ceiling was reached."""

    def __init__(
        self,
        resource: str,
        *,
        limit: float | int,
        used: float | int,
        requested: float | int,
    ) -> None:
        self.resource = resource
        self.limit = limit
        self.used = used
        self.requested = requested
        super().__init__(
            f"Session budget exhausted for {resource}: limit={limit}, "
            f"used={used}, requested={requested}"
        )


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

    Backend permissions are unconditional boundaries. Resource ceilings are
    opt-in: the default empty budget retains the original measurement-only
    research harness, while an RL environment can reserve discrete work before
    its side effect and expose a typed terminal reason when the allowance ends.
    """

    allowed_backends: set[str]
    usage: RunUsage = field(default_factory=RunUsage)
    budget: ResourceBudget = field(default_factory=ResourceBudget)
    terminal_reason: str | None = None
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def _charge(
        self,
        budget_field: str,
        usage_field: str,
        amount: float | int,
    ) -> None:
        ceiling = getattr(self.budget, budget_field)
        used = getattr(self.usage, usage_field)
        if ceiling is not None and used + amount > ceiling:
            self.terminal_reason = f"budget_exhausted:{budget_field}"
            raise BudgetExceeded(
                budget_field,
                limit=ceiling,
                used=used,
                requested=amount,
            )
        setattr(self.usage, usage_field, used + amount)
        if ceiling is not None and used + amount >= ceiling:
            self.terminal_reason = f"budget_exhausted:{budget_field}"

    async def record_search(self, amount: int = 1) -> None:
        async with self._lock:
            self._charge("max_search_queries", "search_calls", amount)

    async def record_exec(self) -> None:
        async with self._lock:
            self._charge("max_exec_calls", "exec_calls", 1)

    async def reserve_llm(
        self,
        amount: int = 1,
        *,
        max_tokens: int | None = None,
    ) -> int | None:
        async with self._lock:
            call_ceiling = self.budget.max_pipeline_llm_calls
            used_calls = self.usage.llm_calls
            if call_ceiling is not None and used_calls + amount > call_ceiling:
                self.terminal_reason = "budget_exhausted:max_pipeline_llm_calls"
                raise BudgetExceeded(
                    "max_pipeline_llm_calls",
                    limit=call_ceiling,
                    used=used_calls,
                    requested=amount,
                )
            ceiling = self.budget.max_pipeline_output_tokens
            if ceiling is None:
                self._charge("max_pipeline_llm_calls", "llm_calls", amount)
                return max_tokens
            remaining = ceiling - self.usage.pipeline_output_tokens_reserved
            if amount <= 0:
                self._charge("max_pipeline_llm_calls", "llm_calls", amount)
                return max_tokens
            per_call = min(max_tokens or 32_000, remaining // amount)
            if per_call < 1:
                self.terminal_reason = "budget_exhausted:max_pipeline_output_tokens"
                raise BudgetExceeded(
                    "max_pipeline_output_tokens",
                    limit=ceiling,
                    used=self.usage.pipeline_output_tokens_reserved,
                    requested=max(amount, 1),
                )
            # Both resources have now been validated. Commit them together so
            # a rejected output-token reservation cannot charge an LLM call
            # whose provider side effect never happened.
            self._charge("max_pipeline_llm_calls", "llm_calls", amount)
            self._charge(
                "max_pipeline_output_tokens",
                "pipeline_output_tokens_reserved",
                per_call * amount,
            )
            return per_call

    async def record_content_fetches(self, requested: int, from_backend: int) -> None:
        """Charge one ``content.*`` call against both fetch counters.

        Taken together they say what a cache saved. ``requested`` follows the
        program's behaviour and ``from_backend`` follows the bill, and the two
        stop being the same number the moment a document is read twice.
        """
        async with self._lock:
            self._charge("max_content_fetches", "content_fetches", requested)
            self.usage.content_backend_fetches += from_backend

    def record_content_backend_fetches(self, amount: int) -> None:
        """Count unique provider leaders after in-flight admission."""

        self.usage.content_backend_fetches += max(amount, 0)

    def record_direct_url_attempt(self) -> None:
        self.usage.direct_url_attempts += 1

    def record_direct_url_success(self) -> None:
        self.usage.direct_url_successes += 1

    async def record_pipeline_model_tokens(self, amount: int) -> None:
        async with self._lock:
            self.usage.pipeline_model_tokens += amount

    def record_provider_attempt(
        self,
        *,
        capability: str,
        attempt: int,
    ) -> None:
        """Record one backend attempt without charging a logical quota.

        Provider callbacks run on the broker event loop and this method has no
        await point, so each update is one atomic event-loop critical section.
        """

        family = capability.strip()
        if not family:
            raise ValueError("provider attempt capability cannot be empty")
        attempts = self.usage.provider_attempts_by_capability
        attempts[family] = attempts.get(family, 0) + 1
        if attempt > 1:
            self.usage.provider_retries += 1

    def record_provider_timing(self, *, phase: str, duration_seconds: float) -> None:
        """Record actual policy wait independently from backend attempts.

        A request may be cancelled while queued, rate limited, or backing off,
        before another backend attempt exists to carry the elapsed time.
        """

        field = {
            "concurrency_queue": "provider_queue_seconds",
            "rate_limit": "provider_rate_limit_wait_seconds",
            "backoff": "provider_backoff_seconds",
        }.get(phase)
        if field is None:
            raise ValueError(f"unknown provider wait phase: {phase!r}")
        setattr(
            self.usage,
            field,
            getattr(self.usage, field) + max(duration_seconds, 0.0),
        )

    def record_deduplicated(self, amount: int) -> None:
        self.usage.intra_call_deduplicated_items += max(amount, 0)

    def record_coalesced(self, amount: int) -> None:
        self.usage.provider_coalesced_requests += max(amount, 0)

    def record_provider_cache(self, *, hit: bool) -> None:
        field = "provider_cache_hits" if hit else "provider_cache_misses"
        setattr(self.usage, field, getattr(self.usage, field) + 1)

    async def record_sandbox_seconds(self, amount: float) -> None:
        async with self._lock:
            self.usage.sandbox_seconds += amount
            ceiling = self.budget.max_sandbox_seconds
            if ceiling is not None and self.usage.sandbox_seconds >= ceiling:
                self.terminal_reason = "budget_exhausted:max_sandbox_seconds"

    async def record_workspace_bytes(self, amount: int) -> None:
        async with self._lock:
            self.usage.workspace_bytes = amount
            ceiling = self.budget.max_workspace_bytes
            if ceiling is not None and amount > ceiling:
                self.terminal_reason = "budget_exhausted:max_workspace_bytes"

    def require_active(self) -> None:
        if self.terminal_reason:
            resource = self.terminal_reason.partition(":")[2] or "session"
            ceiling = getattr(self.budget, resource, 0) or 0
            usage_field = {
                "max_exec_calls": "exec_calls",
                "max_search_queries": "search_calls",
                "max_content_fetches": "content_fetches",
                "max_pipeline_llm_calls": "llm_calls",
                "max_pipeline_output_tokens": "pipeline_output_tokens_reserved",
                "max_sandbox_seconds": "sandbox_seconds",
                "max_workspace_bytes": "workspace_bytes",
            }.get(resource, "exec_calls")
            used = getattr(self.usage, usage_field)
            raise BudgetExceeded(resource, limit=ceiling, used=used, requested=0)

    async def sandbox_timeout(self, deployment_timeout: float) -> float:
        async with self._lock:
            ceiling = self.budget.max_sandbox_seconds
            if ceiling is None:
                return deployment_timeout
            remaining = ceiling - self.usage.sandbox_seconds
            if remaining <= 0:
                self.terminal_reason = "budget_exhausted:max_sandbox_seconds"
                raise BudgetExceeded(
                    "max_sandbox_seconds",
                    limit=ceiling,
                    used=self.usage.sandbox_seconds,
                    requested=deployment_timeout,
                )
            return min(deployment_timeout, remaining)

    def remaining(self) -> dict[str, float | int | None]:
        return budget_remaining(self.budget, self.usage)

    def require_backend(self, backend: str) -> None:
        if backend not in self.allowed_backends:
            raise PermissionError(f"Backend '{backend}' is not enabled for this session")
