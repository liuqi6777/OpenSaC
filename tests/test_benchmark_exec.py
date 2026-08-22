import argparse
import importlib.util
import json
from pathlib import Path

import httpx
import pytest

_SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "benchmark_exec.py"
_SPEC = importlib.util.spec_from_file_location("benchmark_exec", _SCRIPT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
benchmark_exec = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(benchmark_exec)


@pytest.mark.asyncio
async def test_benchmark_level_isolates_warmup_and_records_failures() -> None:
    created = 0
    measured = 0
    submitted_codes: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal created, measured
        if request.method == "POST" and request.url.path == "/v1/sessions":
            created += 1
            return httpx.Response(200, json={"id": f"sess-{created}"})
        if request.method == "POST" and request.url.path.endswith("/exec"):
            payload = json.loads(request.content)
            submitted_codes.append(payload["code"])
            if payload["code"] != "pass\n":
                measured += 1
                if measured == 1:
                    return httpx.Response(503, json={"detail": "saturated"})
            return httpx.Response(
                200,
                json={
                    "succeeded": True,
                    "duration_seconds": 0.01,
                    "timings": {"sandbox_queue_seconds": 0.001},
                    "trace": [],
                },
            )
        if request.method == "DELETE":
            if request.url.path.endswith("sess-1"):
                return httpx.Response(500)
            return httpx.Response(200)
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    async with httpx.AsyncClient(
        base_url="http://test",
        transport=httpx.MockTransport(handler),
    ) as client:
        result = await benchmark_exec._run_level(
            client,
            concurrency=4,
            requests=2,
            warmup_per_worker=1,
            code="print('measure')\n",
            include_trace=True,
        )

    assert created == 2
    assert submitted_codes[:2] == ["pass\n", "pass\n"]
    assert result["requested_concurrency"] == 4
    assert result["effective_concurrency"] == 2
    assert result["attempted_requests"] == 2
    assert result["request_failures"] == 1
    assert result["program_failures"] == 0
    assert result["warmup_failures"] == 0
    assert result["cleanup_failures"] == ["sess-1: HTTP 500"]
    assert result["latency_seconds"]["client_wall_seconds"]["count"] == 2


def test_parse_concurrency_rejects_non_positive_levels() -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        benchmark_exec._parse_concurrency("1,0,4")


def test_health_metrics_extract_resource_peaks() -> None:
    metrics = benchmark_exec._health_metrics(
        {
            "process": {"rss_bytes": 1000, "fd_count": 12},
            "sandbox": {"active": 3, "waiting": 4},
            "broker": {"active": 5, "waiting": 6},
            "provider_cache": {"current_bytes": 2048, "entries": 2, "waiting": 1},
            "warm": {"active": 7, "waiting": 8},
            "sessions": {"active": 9, "executing": 10},
        }
    )

    assert metrics["process.rss_bytes"] == 1000
    assert metrics["sandbox.waiting"] == 4
    assert metrics["broker.active"] == 5
    assert metrics["provider_cache.current_bytes"] == 2048
    assert metrics["provider_cache.entries"] == 2
    assert metrics["warm.waiting"] == 8
    assert metrics["sessions.executing"] == 10


def test_aggregate_repetitions_summarizes_p95_and_throughput() -> None:
    runs = [
        {
            "attempted_requests": 10,
            "request_failures": 1,
            "program_failures": 0,
            "throughput_requests_per_second": 5.0,
            "successful_requests_per_second": 4.5,
            "latency_seconds": {"client_wall_seconds": {"p95": 0.4}},
            "resource_peaks": {"process.rss_bytes": 100.0},
        },
        {
            "attempted_requests": 12,
            "request_failures": 0,
            "program_failures": 1,
            "throughput_requests_per_second": 7.0,
            "successful_requests_per_second": 6.0,
            "latency_seconds": {"client_wall_seconds": {"p95": 0.2}},
            "resource_peaks": {"process.rss_bytes": 120.0},
        },
    ]

    aggregate = benchmark_exec._aggregate_repetitions(8, runs)

    assert aggregate["requested_concurrency"] == 8
    assert aggregate["repetitions"] == 2
    assert aggregate["attempted_requests"] == 22
    assert aggregate["request_failures"] == 1
    assert aggregate["throughput_requests_per_second"]["p50"] == 6.0
    assert aggregate["latency_p95_seconds"]["client_wall_seconds"]["p50"] == pytest.approx(0.3)
    assert aggregate["resource_peaks"]["process.rss_bytes"] == 120.0
