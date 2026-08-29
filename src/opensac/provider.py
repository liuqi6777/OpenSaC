from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Literal, TypeVar

import httpx

ProviderErrorCode = Literal[
    "invalid_request",
    "provider_not_configured",
    "provider_timeout",
    "provider_rate_limited",
    "provider_unavailable",
    "provider_auth_failed",
    "provider_not_found",
    "provider_rejected",
    "provider_http_error",
    "provider_invalid_response",
    "provider_cancelled",
]
AttemptStatus = Literal["success", "error", "cancelled"]
WaitPhase = Literal["concurrency_queue", "rate_limit", "backoff"]
RetryProfile = Literal["none", "safe"]
FailureScope = Literal["request", "resource", "provider", "unknown"]

_NO_TRANSPORT_ERROR_CODES = {"invalid_request", "provider_not_configured"}
_SAFE_RETRY_STATUSES = {408, 429, 500, 502, 503, 504}

T = TypeVar("T")
AttemptObserver = Callable[["ProviderAttempt"], None]
WaitObserver = Callable[["ProviderWait"], None]
Preflight = Callable[[], None]


class ProviderRequestError(Exception):
    """Sanitized, provider-neutral failure raised inside the host runtime."""

    def __init__(
        self,
        code: ProviderErrorCode,
        message: str,
        *,
        retryable: bool,
        provider_status: int | None = None,
        retry_after_seconds: float | None = None,
        attempts: int = 0,
        provider: str | None = None,
        component: str | None = None,
        scope: FailureScope | None = None,
    ) -> None:
        super().__init__(message)
        if attempts < 0:
            raise ValueError("attempts cannot be negative")
        self.code = code
        self.message = message
        self.retryable = retryable
        self.provider_status = provider_status
        self.retry_after_seconds = retry_after_seconds
        self.attempts = attempts
        self.provider = provider
        self.component = component
        self.scope = scope


@dataclass(frozen=True, slots=True)
class ProviderPolicy:
    """Host-owned execution policy bound to one reusable provider service."""

    retry_profile: RetryProfile = "none"
    max_attempts: int = 3
    attempt_timeout_seconds: float = 30.0
    logical_deadline_seconds: float = 90.0
    base_backoff_seconds: float = 0.5
    max_backoff_seconds: float = 4.0
    max_total_backoff_seconds: float = 15.0
    max_retry_after_seconds: float = 15.0
    concurrency: int = 6
    requests_per_second: float | None = None
    burst: int | None = None

    def __post_init__(self) -> None:
        if self.retry_profile not in {"none", "safe"}:
            raise ValueError("retry_profile must be 'none' or 'safe'")
        if not 1 <= self.max_attempts <= 3:
            raise ValueError("max_attempts must be between 1 and 3")
        if self.attempt_timeout_seconds <= 0:
            raise ValueError("attempt_timeout_seconds must be positive")
        if self.logical_deadline_seconds <= 0:
            raise ValueError("logical_deadline_seconds must be positive")
        if self.base_backoff_seconds < 0:
            raise ValueError("base_backoff_seconds cannot be negative")
        if self.max_backoff_seconds < 0:
            raise ValueError("max_backoff_seconds cannot be negative")
        if self.max_total_backoff_seconds < 0:
            raise ValueError("max_total_backoff_seconds cannot be negative")
        if self.max_retry_after_seconds < 0:
            raise ValueError("max_retry_after_seconds cannot be negative")
        if self.concurrency < 1:
            raise ValueError("concurrency must be at least one")
        if self.requests_per_second is not None and self.requests_per_second <= 0:
            raise ValueError("requests_per_second must be positive when configured")
        if self.requests_per_second is None and self.burst is not None:
            raise ValueError("burst requires requests_per_second")
        if self.burst is not None and self.burst < 1:
            raise ValueError("burst must be at least one")

    @property
    def effective_max_attempts(self) -> int:
        return 1 if self.retry_profile == "none" else self.max_attempts


@dataclass(frozen=True, slots=True)
class ProviderAttempt:
    """One real adapter invocation, without request or response bodies."""

    attempt: int
    status: AttemptStatus
    duration_seconds: float
    queue_seconds: float
    rate_limit_wait_seconds: float
    backoff_before_seconds: float
    request_indexes: tuple[int, ...] = ()
    error_code: ProviderErrorCode | None = None
    provider_status: int | None = None


@dataclass(frozen=True, slots=True)
class ProviderWait:
    """Actual host-policy wait, including time spent before cancellation."""

    phase: WaitPhase
    duration_seconds: float
    status: Literal["completed", "cancelled", "deadline"]
    request_indexes: tuple[int, ...] = ()


def parse_retry_after(
    value: str | None,
    *,
    now: datetime | None = None,
) -> float | None:
    """Parse an HTTP Retry-After delta or date without accepting loose numbers."""

    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if value.isascii() and value.isdecimal():
        return float(int(value))
    try:
        retry_at = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if retry_at is None:
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=UTC)
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return max(0.0, (retry_at - current).total_seconds())


def classify_provider_error(
    error: Exception,
    *,
    now: datetime | None = None,
    max_retry_after_seconds: float | None = None,
) -> ProviderRequestError:
    """Convert transport/adapter failures to a body-free internal taxonomy."""

    if isinstance(error, ProviderRequestError):
        retry_after = error.retry_after_seconds
        if retry_after is not None and max_retry_after_seconds is not None:
            retry_after = min(retry_after, max_retry_after_seconds)
        if retry_after == error.retry_after_seconds:
            return error
        return ProviderRequestError(
            error.code,
            error.message,
            retryable=error.retryable,
            provider_status=error.provider_status,
            retry_after_seconds=retry_after,
            attempts=error.attempts,
            provider=error.provider,
            component=error.component,
            scope=error.scope,
        )

    if isinstance(error, (httpx.InvalidURL, httpx.UnsupportedProtocol)):
        return ProviderRequestError(
            "invalid_request",
            "Provider request URL is invalid.",
            retryable=False,
        )

    if isinstance(error, (TimeoutError, httpx.TimeoutException)):
        return ProviderRequestError(
            "provider_timeout",
            "Provider request timed out.",
            retryable=True,
        )

    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        retry_after = None
        if status in _SAFE_RETRY_STATUSES:
            retry_after = parse_retry_after(
                error.response.headers.get("Retry-After"),
                now=now,
            )
            if retry_after is not None and max_retry_after_seconds is not None:
                retry_after = min(retry_after, max_retry_after_seconds)
        if status == 429:
            return ProviderRequestError(
                "provider_rate_limited",
                "Provider rate limit was exceeded.",
                retryable=True,
                provider_status=status,
                retry_after_seconds=retry_after,
            )
        if status == 408:
            return ProviderRequestError(
                "provider_timeout",
                "Provider request timed out.",
                retryable=True,
                provider_status=status,
                retry_after_seconds=retry_after,
            )
        if status in {500, 502, 503, 504}:
            return ProviderRequestError(
                "provider_unavailable",
                "Provider is temporarily unavailable.",
                retryable=True,
                provider_status=status,
                retry_after_seconds=retry_after,
            )
        if status in {401, 403}:
            return ProviderRequestError(
                "provider_auth_failed",
                "Provider rejected its configured credentials or permissions.",
                retryable=False,
                provider_status=status,
            )
        if status == 404:
            return ProviderRequestError(
                "provider_not_found",
                "Provider resource was not found.",
                retryable=False,
                provider_status=status,
            )
        if 400 <= status < 500:
            return ProviderRequestError(
                "provider_rejected",
                "Provider rejected the request.",
                retryable=False,
                provider_status=status,
            )
        return ProviderRequestError(
            "provider_http_error",
            "Provider returned an unexpected HTTP status.",
            retryable=False,
            provider_status=status,
        )

    if isinstance(error, httpx.TransportError):
        return ProviderRequestError(
            "provider_unavailable",
            "Provider could not be reached.",
            retryable=True,
        )

    if isinstance(error, httpx.HTTPError):
        return ProviderRequestError(
            "provider_http_error",
            "Provider HTTP request failed.",
            retryable=False,
        )

    return ProviderRequestError(
        "provider_invalid_response",
        "Provider returned an invalid response.",
        retryable=False,
    )


def infer_failure_scope(
    code: ProviderErrorCode,
    *,
    provider_status: int | None = None,
    resource_failures: bool = False,
) -> FailureScope:
    """Classify the safest actionable layer without inspecting provider bodies."""

    if code in {"invalid_request", "provider_cancelled"}:
        return "request"
    if code in {
        "provider_not_configured",
        "provider_rate_limited",
        "provider_unavailable",
    }:
        return "provider"
    if code == "provider_auth_failed":
        # A 403 alone cannot distinguish account permissions from a restriction
        # on the requested resource. Do not tell a program to rotate a key when
        # the transport status does not support that conclusion.
        if provider_status == 403:
            return "unknown"
        return "provider"
    if code in {"provider_not_found", "provider_rejected"}:
        return "resource" if resource_failures else "provider"
    if code == "provider_invalid_response":
        return "provider"
    return "unknown"


def contextualize_provider_error(
    error: ProviderRequestError,
    *,
    provider: str,
    component: str,
    resource_failures: bool = False,
) -> ProviderRequestError:
    """Attach secret-free provider identity, service label, and actionable scope."""

    return ProviderRequestError(
        error.code,
        error.message,
        retryable=error.retryable,
        provider_status=error.provider_status,
        retry_after_seconds=error.retry_after_seconds,
        attempts=error.attempts,
        provider=error.provider or provider,
        component=error.component or component,
        scope=error.scope
        or infer_failure_scope(
            error.code,
            provider_status=error.provider_status,
            resource_failures=resource_failures,
        ),
    )


class _FifoRateLimiter:
    """A cancellation-safe token bucket with FIFO admission."""

    def __init__(
        self,
        requests_per_second: float,
        burst: int,
        *,
        clock: Callable[[], float],
        sleep: Callable[[float], Awaitable[None]],
    ) -> None:
        self._rate = requests_per_second
        self._capacity = float(burst)
        self._tokens = float(burst)
        self._clock = clock
        self._sleep = sleep
        self._updated_at = clock()
        # asyncio.Lock grants acquisition to queued tasks in arrival order.
        self._lock = asyncio.Lock()

    async def acquire(self, *, max_wait: float) -> float:
        queued_at = self._clock()
        async with self._lock:
            while True:
                now = self._clock()
                elapsed = max(0.0, now - self._updated_at)
                self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
                self._updated_at = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return max(0.0, self._clock() - queued_at)

                token_wait = (1.0 - self._tokens) / self._rate
                elapsed_in_queue = max(0.0, self._clock() - queued_at)
                if elapsed_in_queue + token_wait > max_wait:
                    raise _logical_deadline_error()
                # Keep the fair asyncio lock while sleeping. A cancelled
                # waiter exits before consuming a token, and the next waiter
                # refills from elapsed clock time instead of inheriting a
                # leaked reservation.
                await self._sleep(token_wait)


@dataclass(slots=True)
class _ProviderGovernor:
    concurrency: asyncio.Semaphore
    rate_limiter: _FifoRateLimiter | None
    active: int = 0
    waiting: int = 0
    admitted: int = 0

    async def acquire(self, *, timeout: float) -> None:
        self.waiting += 1
        try:
            await asyncio.wait_for(self.concurrency.acquire(), timeout=timeout)
        finally:
            self.waiting -= 1
        self.active += 1
        self.admitted += 1

    def release(self) -> None:
        if self.active < 1:
            raise RuntimeError("provider governor released without an active request")
        self.active -= 1
        self.concurrency.release()


class ProviderRuntime:
    """Execute backend requests under a reusable host-owned policy."""

    def __init__(
        self,
        policy: ProviderPolicy | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: Callable[[], float] | None = None,
        wall_clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.policy = policy or ProviderPolicy()
        self._clock = clock
        self._sleep = sleep
        self._rng = rng or random.random
        self._wall_clock = wall_clock or (lambda: datetime.now(UTC))
        self._governors: dict[str, _ProviderGovernor] = {}

    def snapshot(self, provider_identity: str) -> dict[str, int]:
        """Return live capacity state for one backend bound to this runtime."""

        governor = self._governors.get(provider_identity)
        return {
            "capacity": self.policy.concurrency,
            "active": governor.active if governor is not None else 0,
            "waiting": governor.waiting if governor is not None else 0,
            "admitted": governor.admitted if governor is not None else 0,
        }

    def _governor(
        self,
        provider_identity: str,
    ) -> _ProviderGovernor:
        governor = self._governors.get(provider_identity)
        if governor is not None:
            return governor
        policy = self.policy
        limiter = None
        if policy.requests_per_second is not None:
            limiter = _FifoRateLimiter(
                policy.requests_per_second,
                policy.burst or 1,
                clock=self._clock,
                sleep=self._sleep,
            )
        governor = _ProviderGovernor(
            concurrency=asyncio.Semaphore(policy.concurrency),
            rate_limiter=limiter,
        )
        self._governors[provider_identity] = governor
        return governor

    async def run(
        self,
        request: Callable[[], Awaitable[T]],
        *,
        provider_identity: str = "default",
        request_indexes: Sequence[int] = (),
        preflight: Preflight | None = None,
        observer: AttemptObserver | None = None,
        wait_observer: WaitObserver | None = None,
    ) -> T:
        """Run a fresh request coroutine per attempt and return its normalized result."""

        if not provider_identity:
            raise ValueError("provider_identity cannot be empty")
        policy = self.policy
        if preflight is not None:
            try:
                preflight()
            except Exception as exc:
                failure = classify_provider_error(
                    exc,
                    now=self._wall_clock(),
                    max_retry_after_seconds=policy.max_retry_after_seconds,
                )
                raise _with_attempts(failure, 0) from exc

        # Do not even instantiate a governor until synchronous adapter
        # validation succeeds. Besides avoiding needless queueing, this keeps a
        # bad request or missing deployment credential from consuming an RPS
        # token that belongs to a real backend attempt.
        governor = self._governor(provider_identity)
        logical_started = self._clock()
        total_backoff = 0.0
        backoff_before = 0.0
        attempts_started = 0
        indexes = tuple(request_indexes)

        for attempt in range(1, policy.effective_max_attempts + 1):
            remaining = self._remaining(policy, logical_started)
            if remaining <= 0:
                raise _logical_deadline_error(attempts=attempts_started)

            queue_started = self._clock()
            try:
                await governor.acquire(timeout=remaining)
            except asyncio.CancelledError:
                self._observe_wait(
                    wait_observer,
                    ProviderWait(
                        phase="concurrency_queue",
                        duration_seconds=max(0.0, self._clock() - queue_started),
                        status="cancelled",
                        request_indexes=indexes,
                    ),
                )
                raise
            except TimeoutError as exc:
                self._observe_wait(
                    wait_observer,
                    ProviderWait(
                        phase="concurrency_queue",
                        duration_seconds=max(0.0, self._clock() - queue_started),
                        status="deadline",
                        request_indexes=indexes,
                    ),
                )
                raise _logical_deadline_error(attempts=attempts_started) from exc
            try:
                queue_seconds = max(0.0, self._clock() - queue_started)
                self._observe_wait(
                    wait_observer,
                    ProviderWait(
                        phase="concurrency_queue",
                        duration_seconds=queue_seconds,
                        status="completed",
                        request_indexes=indexes,
                    ),
                )

                remaining = self._remaining(policy, logical_started)
                if remaining <= 0:
                    raise _logical_deadline_error(attempts=attempts_started)

                rate_limit_wait = 0.0
                if governor.rate_limiter is not None:
                    rate_limit_started = self._clock()
                    try:
                        rate_limit_wait = await governor.rate_limiter.acquire(max_wait=remaining)
                    except ProviderRequestError as exc:
                        self._observe_wait(
                            wait_observer,
                            ProviderWait(
                                phase="rate_limit",
                                duration_seconds=max(
                                    0.0,
                                    self._clock() - rate_limit_started,
                                ),
                                status="deadline",
                                request_indexes=indexes,
                            ),
                        )
                        raise _with_attempts(exc, attempts_started) from exc
                    except asyncio.CancelledError:
                        self._observe_wait(
                            wait_observer,
                            ProviderWait(
                                phase="rate_limit",
                                duration_seconds=max(
                                    0.0,
                                    self._clock() - rate_limit_started,
                                ),
                                status="cancelled",
                                request_indexes=indexes,
                            ),
                        )
                        raise
                    self._observe_wait(
                        wait_observer,
                        ProviderWait(
                            phase="rate_limit",
                            duration_seconds=rate_limit_wait,
                            status="completed",
                            request_indexes=indexes,
                        ),
                    )
                    remaining = self._remaining(policy, logical_started)
                    if remaining <= 0:
                        raise _logical_deadline_error(attempts=attempts_started)

                call_started = self._clock()
                status: AttemptStatus = "success"
                provider_error: ProviderRequestError | None = None
                try:
                    timeout = min(policy.attempt_timeout_seconds, remaining)
                    result = await asyncio.wait_for(request(), timeout=timeout)
                except asyncio.CancelledError:
                    attempts_started += 1
                    status = "cancelled"
                    self._observe(
                        observer,
                        ProviderAttempt(
                            attempt=attempts_started,
                            status=status,
                            duration_seconds=max(0.0, self._clock() - call_started),
                            queue_seconds=queue_seconds,
                            rate_limit_wait_seconds=rate_limit_wait,
                            backoff_before_seconds=backoff_before,
                            request_indexes=indexes,
                            error_code="provider_cancelled",
                        ),
                    )
                    raise
                except Exception as exc:
                    status = "error"
                    provider_error = classify_provider_error(
                        exc,
                        now=self._wall_clock(),
                        max_retry_after_seconds=policy.max_retry_after_seconds,
                    )
                    if provider_error.code not in _NO_TRANSPORT_ERROR_CODES:
                        attempts_started += 1
            finally:
                governor.release()

            duration = max(0.0, self._clock() - call_started)
            if provider_error is None:
                attempts_started += 1
                self._observe(
                    observer,
                    ProviderAttempt(
                        attempt=attempts_started,
                        status=status,
                        duration_seconds=duration,
                        queue_seconds=queue_seconds,
                        rate_limit_wait_seconds=rate_limit_wait,
                        backoff_before_seconds=backoff_before,
                        request_indexes=indexes,
                    ),
                )
                return result

            if provider_error.code not in _NO_TRANSPORT_ERROR_CODES:
                self._observe(
                    observer,
                    ProviderAttempt(
                        attempt=attempts_started,
                        status=status,
                        duration_seconds=duration,
                        queue_seconds=queue_seconds,
                        rate_limit_wait_seconds=rate_limit_wait,
                        backoff_before_seconds=backoff_before,
                        request_indexes=indexes,
                        error_code=provider_error.code,
                        provider_status=provider_error.provider_status,
                    ),
                )
            if not provider_error.retryable or attempt >= policy.effective_max_attempts:
                raise _with_attempts(provider_error, attempts_started) from provider_error

            retry_delay = self._retry_delay(policy, attempt, provider_error)
            remaining = self._remaining(policy, logical_started)
            if (
                total_backoff + retry_delay > policy.max_total_backoff_seconds
                or retry_delay >= remaining
            ):
                raise _with_attempts(provider_error, attempts_started) from provider_error
            if retry_delay:
                backoff_started = self._clock()
                try:
                    await self._sleep(retry_delay)
                except asyncio.CancelledError:
                    self._observe_wait(
                        wait_observer,
                        ProviderWait(
                            phase="backoff",
                            duration_seconds=max(
                                0.0,
                                self._clock() - backoff_started,
                            ),
                            status="cancelled",
                            request_indexes=indexes,
                        ),
                    )
                    raise
                actual_backoff = max(0.0, self._clock() - backoff_started)
                self._observe_wait(
                    wait_observer,
                    ProviderWait(
                        phase="backoff",
                        duration_seconds=actual_backoff,
                        status="completed",
                        request_indexes=indexes,
                    ),
                )
            else:
                actual_backoff = 0.0
            total_backoff += retry_delay
            backoff_before = actual_backoff

        raise AssertionError("provider attempt loop exited without a result")

    def _remaining(self, policy: ProviderPolicy, started: float) -> float:
        elapsed = max(0.0, self._clock() - started)
        return policy.logical_deadline_seconds - elapsed

    def _retry_delay(
        self,
        policy: ProviderPolicy,
        attempt: int,
        error: ProviderRequestError,
    ) -> float:
        exponential_cap = min(
            policy.max_backoff_seconds,
            policy.base_backoff_seconds * (2 ** (attempt - 1)),
        )
        jitter = min(1.0, max(0.0, float(self._rng()))) * exponential_cap
        return max(jitter, error.retry_after_seconds or 0.0)

    @staticmethod
    def _observe(observer: AttemptObserver | None, attempt: ProviderAttempt) -> None:
        if observer is None:
            return
        observer(attempt)

    @staticmethod
    def _observe_wait(observer: WaitObserver | None, wait: ProviderWait) -> None:
        if observer is None:
            return
        observer(wait)


def invalid_provider_response() -> ProviderRequestError:
    """Construct the common adapter validation failure without payload details."""

    return ProviderRequestError(
        "provider_invalid_response",
        "Provider returned an invalid response.",
        retryable=False,
    )


def _with_attempts(
    error: ProviderRequestError,
    attempts: int,
) -> ProviderRequestError:
    return ProviderRequestError(
        error.code,
        error.message,
        retryable=error.retryable,
        provider_status=error.provider_status,
        retry_after_seconds=error.retry_after_seconds,
        attempts=attempts,
        provider=error.provider,
        component=error.component,
        scope=error.scope,
    )


def _logical_deadline_error(*, attempts: int = 0) -> ProviderRequestError:
    return ProviderRequestError(
        "provider_timeout",
        "Provider logical request deadline was exceeded.",
        retryable=True,
        attempts=attempts,
    )
