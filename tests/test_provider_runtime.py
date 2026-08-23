from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest

from opensac.provider import (
    ProviderAttempt,
    ProviderPolicy,
    ProviderRequestError,
    ProviderRuntime,
    ProviderWait,
    classify_provider_error,
    contextualize_provider_error,
    parse_retry_after,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.now += delay
        await asyncio.sleep(0)


def status_error(status: int, *, retry_after: str | None = None) -> httpx.HTTPStatusError:
    headers = {"Retry-After": retry_after} if retry_after is not None else None
    request = httpx.Request("POST", "https://provider.invalid/operation")
    response = httpx.Response(
        status,
        headers=headers,
        request=request,
        text="secret provider response body",
    )
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return exc
    raise AssertionError("expected an HTTP status error")


@pytest.mark.parametrize(
    ("error", "code", "retryable", "status"),
    [
        (httpx.ConnectError("secret endpoint"), "provider_unavailable", True, None),
        (httpx.ReadTimeout("secret endpoint"), "provider_timeout", True, None),
        (status_error(408), "provider_timeout", True, 408),
        (status_error(429), "provider_rate_limited", True, 429),
        (status_error(503), "provider_unavailable", True, 503),
        (status_error(401), "provider_auth_failed", False, 401),
        (status_error(404), "provider_not_found", False, 404),
        (status_error(422), "provider_rejected", False, 422),
        (status_error(599), "provider_http_error", False, 599),
        (httpx.HTTPError("secret HTTP detail"), "provider_http_error", False, None),
        (ValueError("secret payload"), "provider_invalid_response", False, None),
    ],
)
def test_classifier_uses_stable_sanitized_failures(
    error: Exception,
    code: str,
    retryable: bool,
    status: int | None,
) -> None:
    failure = classify_provider_error(error)

    assert failure.code == code
    assert failure.retryable is retryable
    assert failure.provider_status == status
    assert "secret" not in failure.message
    assert "secret" not in str(failure)


@pytest.mark.parametrize(
    ("code", "operation", "status", "scope"),
    [
        ("invalid_request", "web.scrape", None, "request"),
        ("provider_not_found", "web.scrape", 404, "resource"),
        ("provider_rejected", "local.document", 422, "resource"),
        ("provider_auth_failed", "web.scrape", 403, "unknown"),
        ("provider_auth_failed", "web.scrape", 401, "provider"),
        ("provider_unavailable", "web.scrape", 503, "provider"),
        ("provider_timeout", "web.scrape", 408, "unknown"),
        ("provider_invalid_response", "web.rerank", None, "provider"),
    ],
)
def test_provider_context_identifies_the_actionable_failure_layer(
    code,
    operation,
    status,
    scope,
) -> None:
    failure = contextualize_provider_error(
        ProviderRequestError(
            code,
            "Sanitized provider failure.",
            retryable=False,
            provider_status=status,
        ),
        provider="jina_reader",
        operation=operation,
    )

    assert failure.provider == "jina_reader"
    assert failure.operation == operation
    assert failure.scope == scope


def test_retry_after_accepts_delta_or_http_date_and_rejects_loose_values() -> None:
    now = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)

    assert parse_retry_after("12", now=now) == 12.0
    assert parse_retry_after(format_datetime(now + timedelta(seconds=7)), now=now) == 7.0
    assert parse_retry_after(format_datetime(now - timedelta(seconds=7)), now=now) == 0.0
    assert parse_retry_after("1.5", now=now) is None
    assert parse_retry_after("not a date", now=now) is None


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
def test_retry_after_is_available_on_every_safe_retry_status(status: int) -> None:
    failure = classify_provider_error(
        status_error(status, retry_after="120"),
        max_retry_after_seconds=15.0,
    )

    assert failure.retryable is True
    assert failure.retry_after_seconds == 15.0


def test_retry_after_date_and_invalid_value_on_unavailable_response() -> None:
    now = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)
    valid = status_error(
        503,
        retry_after=format_datetime(now + timedelta(seconds=7)),
    )

    assert classify_provider_error(valid, now=now).retry_after_seconds == 7.0
    assert (
        classify_provider_error(
            status_error(503, retry_after="not a date"),
            now=now,
        ).retry_after_seconds
        is None
    )


async def test_none_profile_never_retries() -> None:
    attempts: list[ProviderAttempt] = []
    calls = 0
    runtime = ProviderRuntime(
        default_policy=ProviderPolicy(retry_profile="none", max_attempts=3)
    )

    async def request() -> str:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("provider URL and secret body")

    with pytest.raises(ProviderRequestError) as caught:
        await runtime.run("web.search", request, observer=attempts.append)

    assert caught.value.code == "provider_unavailable"
    assert caught.value.attempts == 1
    assert calls == 1
    assert [(row.attempt, row.status) for row in attempts] == [(1, "error")]


async def test_safe_profile_retries_with_bounded_backoff_and_attempt_records() -> None:
    clock = FakeClock()
    attempts: list[ProviderAttempt] = []
    calls = 0
    runtime = ProviderRuntime(
        default_policy=ProviderPolicy(
            retry_profile="safe",
            max_attempts=3,
            base_backoff_seconds=0.25,
            max_backoff_seconds=1.0,
            max_total_backoff_seconds=2.0,
        ),
        clock=clock,
        sleep=clock.sleep,
        rng=lambda: 1.0,
    )

    async def request() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise status_error(503)
        return "ok"

    result = await runtime.run(
        "web.search",
        request,
        request_indexes=(2, 5),
        observer=attempts.append,
    )

    assert result == "ok"
    assert calls == 2
    assert clock.sleeps == [0.25]
    assert [row.status for row in attempts] == ["error", "success"]
    assert [row.attempt for row in attempts] == [1, 2]
    assert attempts[0].provider_status == 503
    assert attempts[0].error_code == "provider_unavailable"
    assert attempts[1].backoff_before_seconds == 0.25
    assert attempts[1].request_indexes == (2, 5)


async def test_attempt_trace_uses_actual_backoff_elapsed_time() -> None:
    clock = FakeClock()
    attempts: list[ProviderAttempt] = []
    waits: list[ProviderWait] = []
    calls = 0

    async def oversleep(delay: float) -> None:
        clock.sleeps.append(delay)
        clock.now += 3.0
        await asyncio.sleep(0)

    runtime = ProviderRuntime(
        default_policy=ProviderPolicy(
            retry_profile="safe",
            max_attempts=2,
            base_backoff_seconds=1.0,
        ),
        clock=clock,
        sleep=oversleep,
        rng=lambda: 1.0,
    )

    async def request() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise status_error(503)
        return "ok"

    assert (
        await runtime.run(
            "web.search",
            request,
            observer=attempts.append,
            wait_observer=waits.append,
        )
        == "ok"
    )
    assert clock.sleeps == [1.0]
    assert attempts[1].backoff_before_seconds == 3.0
    assert [wait.duration_seconds for wait in waits if wait.phase == "backoff"] == [
        3.0
    ]


async def test_retry_after_is_clamped_and_respected() -> None:
    clock = FakeClock()
    attempts: list[ProviderAttempt] = []
    calls = 0
    runtime = ProviderRuntime(
        default_policy=ProviderPolicy(
            retry_profile="safe",
            max_attempts=2,
            base_backoff_seconds=0.1,
            max_total_backoff_seconds=3.0,
            max_retry_after_seconds=2.0,
        ),
        clock=clock,
        sleep=clock.sleep,
        rng=lambda: 0.0,
    )

    async def request() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise status_error(503, retry_after="120")
        return "ok"

    assert await runtime.run("web.search", request, observer=attempts.append) == "ok"
    assert clock.sleeps == [2.0]
    assert attempts[0].error_code == "provider_unavailable"
    assert attempts[0].provider_status == 503
    assert attempts[1].backoff_before_seconds == 2.0


async def test_final_error_reports_transport_attempts_and_clamped_retry_after() -> None:
    attempts: list[ProviderAttempt] = []
    runtime = ProviderRuntime(
        default_policy=ProviderPolicy(
            retry_profile="safe",
            max_attempts=3,
            max_total_backoff_seconds=0.0,
            max_retry_after_seconds=15.0,
        )
    )

    async def request() -> None:
        raise status_error(429, retry_after="120")

    with pytest.raises(ProviderRequestError) as caught:
        await runtime.run("web.search", request, observer=attempts.append)

    assert caught.value.code == "provider_rate_limited"
    assert caught.value.attempts == 1
    assert caught.value.retry_after_seconds == 15.0
    assert len(attempts) == 1


async def test_permanent_error_and_backoff_budget_stop_before_another_side_effect() -> None:
    clock = FakeClock()
    calls = 0
    runtime = ProviderRuntime(
        default_policy=ProviderPolicy(
            retry_profile="safe",
            max_attempts=3,
            base_backoff_seconds=1.0,
            max_total_backoff_seconds=0.5,
        ),
        clock=clock,
        sleep=clock.sleep,
        rng=lambda: 1.0,
    )

    async def unavailable() -> None:
        nonlocal calls
        calls += 1
        raise status_error(503)

    with pytest.raises(ProviderRequestError):
        await runtime.run("local.search", unavailable)
    assert calls == 1
    assert clock.sleeps == []

    calls = 0

    async def rejected() -> None:
        nonlocal calls
        calls += 1
        raise status_error(400)

    with pytest.raises(ProviderRequestError):
        await runtime.run("local.document", rejected)
    assert calls == 1


async def test_logical_deadline_stops_before_the_next_retry_side_effect() -> None:
    clock = FakeClock()
    calls = 0
    runtime = ProviderRuntime(
        default_policy=ProviderPolicy(
            retry_profile="safe",
            max_attempts=3,
            logical_deadline_seconds=0.5,
            base_backoff_seconds=1.0,
        ),
        clock=clock,
        sleep=clock.sleep,
        rng=lambda: 1.0,
    )

    async def unavailable() -> None:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("provider unavailable")

    with pytest.raises(ProviderRequestError) as caught:
        await runtime.run("web.search", unavailable)

    assert caught.value.attempts == 1
    assert calls == 1
    assert clock.sleeps == []


async def test_attempt_timeout_and_task_cancellation_are_observed() -> None:
    timeout_attempts: list[ProviderAttempt] = []
    runtime = ProviderRuntime(
        default_policy=ProviderPolicy(
            retry_profile="none",
            attempt_timeout_seconds=0.01,
            logical_deadline_seconds=1.0,
        )
    )

    async def hangs() -> None:
        await asyncio.Event().wait()

    with pytest.raises(ProviderRequestError) as caught:
        await runtime.run("local.document", hangs, observer=timeout_attempts.append)
    assert caught.value.code == "provider_timeout"
    assert caught.value.attempts == 1
    assert timeout_attempts[0].status == "error"
    assert timeout_attempts[0].error_code == "provider_timeout"

    cancelled_attempts: list[ProviderAttempt] = []
    started = asyncio.Event()

    async def cancellable() -> None:
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(
        runtime.run("web.scrape", cancellable, observer=cancelled_attempts.append)
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled_attempts[0].status == "cancelled"
    assert cancelled_attempts[0].error_code == "provider_cancelled"


async def test_concurrency_is_scoped_per_operation() -> None:
    policy = ProviderPolicy(concurrency=1)
    runtime = ProviderRuntime(
        {"local.search": policy, "local.document": policy},
        default_policy=policy,
    )
    search_started = asyncio.Event()
    release_search = asyncio.Event()
    second_search_started = asyncio.Event()
    document_started = asyncio.Event()

    async def first_search() -> str:
        search_started.set()
        await release_search.wait()
        return "first"

    async def second_search() -> str:
        second_search_started.set()
        return "second"

    async def document() -> str:
        document_started.set()
        return "document"

    first = asyncio.create_task(runtime.run("local.search", first_search))
    await search_started.wait()
    second = asyncio.create_task(runtime.run("local.search", second_search))
    other_operation = asyncio.create_task(runtime.run("local.document", document))
    await document_started.wait()
    assert second_search_started.is_set() is False

    release_search.set()
    assert await asyncio.gather(first, second, other_operation) == [
        "first",
        "second",
        "document",
    ]


async def test_fifo_rate_limiter_preserves_order_and_reports_wait() -> None:
    clock = FakeClock()
    attempts: list[ProviderAttempt] = []
    runtime = ProviderRuntime(
        default_policy=ProviderPolicy(
            concurrency=3,
            requests_per_second=2.0,
            burst=1,
        ),
        clock=clock,
        sleep=clock.sleep,
    )
    completed: list[int] = []

    async def run_one(index: int) -> int:
        async def request() -> int:
            completed.append(index)
            return index

        return await runtime.run(
            "web.scrape",
            request,
            request_indexes=(index,),
            observer=attempts.append,
        )

    assert await asyncio.gather(*(run_one(index) for index in range(3))) == [0, 1, 2]
    assert completed == [0, 1, 2]
    assert [row.rate_limit_wait_seconds for row in attempts] == [0.0, 0.5, 0.5]
    assert clock.sleeps == [0.5, 0.5]


async def test_rate_limiter_cancellation_does_not_leak_a_token() -> None:
    class CancelFirstWait:
        def __init__(self) -> None:
            self.clock = FakeClock()
            self.waiting = asyncio.Event()
            self.calls = 0

        async def __call__(self, delay: float) -> None:
            self.calls += 1
            if self.calls == 1:
                self.waiting.set()
                await asyncio.Event().wait()
            self.clock.now += delay
            await asyncio.sleep(0)

    sleeps = CancelFirstWait()
    attempts: list[ProviderAttempt] = []
    runtime = ProviderRuntime(
        default_policy=ProviderPolicy(
            requests_per_second=1.0,
            burst=1,
            logical_deadline_seconds=10.0,
        ),
        clock=sleeps.clock,
        sleep=sleeps,
    )

    async def request() -> str:
        return "ok"

    assert await runtime.run("web.scrape", request) == "ok"
    cancelled = asyncio.create_task(runtime.run("web.scrape", request))
    await sleeps.waiting.wait()
    follower = asyncio.create_task(
        runtime.run("web.scrape", request, observer=attempts.append)
    )
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled

    assert await follower == "ok"
    # One second, not two: the cancelled waiter never reserved a token.
    assert attempts[0].rate_limit_wait_seconds == 1.0


async def test_cancelled_backoff_reports_actual_wait_without_synthetic_attempt() -> None:
    waiting = asyncio.Event()
    release = asyncio.Event()
    attempts: list[ProviderAttempt] = []
    waits: list[ProviderWait] = []

    async def blocking_sleep(_delay: float) -> None:
        waiting.set()
        await release.wait()

    runtime = ProviderRuntime(
        default_policy=ProviderPolicy(
            retry_profile="safe",
            max_attempts=2,
            base_backoff_seconds=1.0,
        ),
        sleep=blocking_sleep,
        rng=lambda: 1.0,
    )

    async def unavailable() -> None:
        raise status_error(503)

    task = asyncio.create_task(
        runtime.run(
            "web.search",
            unavailable,
            observer=attempts.append,
            wait_observer=waits.append,
        )
    )
    await waiting.wait()
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(attempts) == 1
    backoff = [row for row in waits if row.phase == "backoff"]
    assert len(backoff) == 1
    assert backoff[0].status == "cancelled"
    assert backoff[0].duration_seconds > 0


async def test_cancelled_concurrency_queue_reports_actual_wait() -> None:
    occupied = asyncio.Event()
    release = asyncio.Event()
    waits: list[ProviderWait] = []
    runtime = ProviderRuntime(default_policy=ProviderPolicy(concurrency=1))

    async def leader() -> None:
        occupied.set()
        await release.wait()

    first = asyncio.create_task(runtime.run("local.document", leader))
    await occupied.wait()
    queued = asyncio.create_task(
        runtime.run(
            "local.document",
            lambda: asyncio.sleep(0),
            wait_observer=waits.append,
        )
    )
    await asyncio.sleep(0.01)
    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued
    release.set()
    await first

    queue_waits = [row for row in waits if row.phase == "concurrency_queue"]
    assert len(queue_waits) == 1
    assert queue_waits[0].status == "cancelled"
    assert queue_waits[0].duration_seconds > 0


async def test_cancelled_rate_limit_wait_reports_actual_wait() -> None:
    waiting = asyncio.Event()
    release = asyncio.Event()
    waits: list[ProviderWait] = []

    async def blocking_sleep(_delay: float) -> None:
        waiting.set()
        await release.wait()

    runtime = ProviderRuntime(
        default_policy=ProviderPolicy(
            concurrency=2,
            requests_per_second=1.0,
            burst=1,
        ),
        sleep=blocking_sleep,
    )

    async def request() -> str:
        return "ok"

    assert await runtime.run("web.scrape", request) == "ok"
    queued = asyncio.create_task(
        runtime.run(
            "web.scrape",
            request,
            wait_observer=waits.append,
        )
    )
    await waiting.wait()
    await asyncio.sleep(0.01)
    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued

    rate_waits = [row for row in waits if row.phase == "rate_limit"]
    assert len(rate_waits) == 1
    assert rate_waits[0].status == "cancelled"
    assert rate_waits[0].duration_seconds > 0


async def test_preflight_failures_do_not_enter_governor_or_create_request() -> None:
    clock = FakeClock()
    attempts: list[ProviderAttempt] = []
    waits: list[ProviderWait] = []
    request_calls = 0
    preflight_calls = 0
    runtime = ProviderRuntime(
        default_policy=ProviderPolicy(
            retry_profile="safe",
            max_attempts=3,
            requests_per_second=1.0,
            burst=1,
        ),
        clock=clock,
        sleep=clock.sleep,
    )

    async def request() -> str:
        nonlocal request_calls
        request_calls += 1
        return "ok"

    for code in ("provider_not_configured", "invalid_request"):

        def reject(code: str = code) -> None:
            nonlocal preflight_calls
            preflight_calls += 1
            raise ProviderRequestError(
                code,
                "Provider request failed preflight validation.",
                retryable=False,
            )

        with pytest.raises(ProviderRequestError) as caught:
            await runtime.run(
                "web.search",
                request,
                preflight=reject,
                observer=attempts.append,
                wait_observer=waits.append,
            )
        assert caught.value.code == code
        assert caught.value.attempts == 0

    assert preflight_calls == 2
    assert request_calls == 0
    assert attempts == []
    assert waits == []
    assert clock.sleeps == []
    assert runtime._governors == {}

    # The first real transport remains immediately admissible, proving neither
    # rejected preflight consumed the initial burst token.
    assert await runtime.run("web.search", request) == "ok"
    assert request_calls == 1
    assert clock.sleeps == []


async def test_governor_identity_is_separate_from_canonical_trace_operation() -> None:
    runtime = ProviderRuntime(default_policy=ProviderPolicy(concurrency=1))
    first_started = asyncio.Event()
    first_release = asyncio.Event()
    second_started = asyncio.Event()
    attempts: list[ProviderAttempt] = []

    async def first_request() -> None:
        first_started.set()
        await first_release.wait()

    async def second_request() -> None:
        second_started.set()

    first = asyncio.create_task(
        runtime.run(
            "web.search",
            first_request,
            provider_identity="endpoint-and-credential-a",
        )
    )
    await first_started.wait()
    second = asyncio.create_task(
        runtime.run(
            "web.search",
            second_request,
            provider_identity="endpoint-and-credential-b",
            observer=attempts.append,
        )
    )
    await second_started.wait()
    first_release.set()
    await asyncio.gather(first, second)

    assert attempts[0].operation == "web.search"


def test_default_policy_matches_the_frozen_host_limits() -> None:
    policy = ProviderPolicy()

    assert policy.logical_deadline_seconds == 90.0
    assert policy.base_backoff_seconds == 0.5
    assert policy.max_backoff_seconds == 4.0
    assert policy.max_total_backoff_seconds == 15.0
    assert policy.max_retry_after_seconds == 15.0


def test_policy_rejects_unsafe_or_ambiguous_limits() -> None:
    with pytest.raises(ValueError, match="between 1 and 3"):
        ProviderPolicy(max_attempts=4)
    with pytest.raises(ValueError, match="burst requires"):
        ProviderPolicy(burst=2)
    with pytest.raises(ValueError, match="positive"):
        ProviderPolicy(requests_per_second=0)
