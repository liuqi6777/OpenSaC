#!/usr/bin/env python3
"""Measure OpenSAC /exec throughput and phase latency at several concurrency levels."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import statistics
import time
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any

import httpx


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _summary(values: list[float]) -> dict[str, float | int]:
    if not values:
        return {"count": 0}
    return {
        "count": len(values),
        "mean": statistics.fmean(values),
        "p50": _percentile(values, 0.50),
        "p95": _percentile(values, 0.95),
        "p99": _percentile(values, 0.99),
        "max": max(values),
    }


def _parse_concurrency(value: str) -> list[int]:
    levels = [int(item.strip()) for item in value.split(",") if item.strip()]
    if not levels or any(level < 1 for level in levels):
        raise argparse.ArgumentTypeError("concurrency must be a comma-separated list of integers")
    return levels


async def _create_session(client: httpx.AsyncClient, backend: str) -> str:
    response = await client.post("/v1/sessions", json={"backends": [backend]})
    response.raise_for_status()
    return str(response.json()["id"])


async def _delete_session(client: httpx.AsyncClient, session_id: str) -> str | None:
    try:
        response = await client.delete(f"/v1/sessions/{session_id}")
        if response.status_code not in {200, 404}:
            return f"{session_id}: HTTP {response.status_code}"
    except httpx.HTTPError as exc:
        return f"{session_id}: {type(exc).__name__}: {exc}"
    return None


async def _exec(
    client: httpx.AsyncClient,
    session_id: str,
    code: str,
    execution_id: str,
    *,
    include_trace: bool,
) -> dict[str, Any]:
    started = time.monotonic()
    try:
        response = await client.post(
            f"/v1/sessions/{session_id}/exec",
            json={
                "code": code,
                "include_trace": include_trace,
                "exec_id": execution_id,
            },
        )
        client_wall_seconds = time.monotonic() - started
        if response.is_error:
            return {
                "request_ok": False,
                "status_code": response.status_code,
                "client_wall_seconds": client_wall_seconds,
            }
        result = response.json()
        result.update(
            request_ok=True,
            status_code=response.status_code,
            client_wall_seconds=client_wall_seconds,
        )
        return result
    except httpx.HTTPError as exc:
        return {
            "request_ok": False,
            "transport_error": type(exc).__name__,
            "client_wall_seconds": time.monotonic() - started,
        }


async def _run_level(
    client: httpx.AsyncClient,
    *,
    concurrency: int,
    requests: int,
    warmup_per_worker: int,
    backend: str,
    code: str,
    include_trace: bool,
) -> dict[str, Any]:
    worker_count = min(concurrency, requests)
    sessions = await asyncio.gather(
        *(_create_session(client, backend) for _ in range(worker_count))
    )
    run_token = uuid.uuid4().hex
    records: list[dict[str, Any]] = []
    warmup_failures = 0

    async def warm_worker(worker_index: int) -> None:
        nonlocal warmup_failures
        session_id = sessions[worker_index]
        for warmup_index in range(warmup_per_worker):
            record = await _exec(
                client,
                session_id,
                "pass\n",
                f"bench-{run_token}-warmup-{worker_index}-{warmup_index}",
                include_trace=False,
            )
            if not record.get("request_ok", False):
                warmup_failures += 1

    async def worker(worker_index: int) -> None:
        session_id = sessions[worker_index]
        for request_index in range(worker_index, requests, worker_count):
            record = await _exec(
                client,
                session_id,
                code,
                f"bench-{run_token}-{request_index}",
                include_trace=include_trace,
            )
            records.append(record)

    try:
        await asyncio.gather(*(warm_worker(index) for index in range(worker_count)))
        started = time.monotonic()
        await asyncio.gather(*(worker(index) for index in range(worker_count)))
        elapsed = time.monotonic() - started
    finally:
        cleanup_results = await asyncio.gather(
            *(_delete_session(client, session_id) for session_id in sessions),
        )

    samples: dict[str, list[float]] = defaultdict(list)
    capability_samples: dict[str, list[float]] = defaultdict(list)
    capability_counts: dict[str, int] = defaultdict(int)
    request_failures = 0
    program_failures = 0
    for record in records:
        samples["client_wall_seconds"].append(float(record["client_wall_seconds"]))
        if not record.get("request_ok", False):
            request_failures += 1
            continue
        samples["sandbox_duration_seconds"].append(float(record.get("duration_seconds", 0.0)))
        for name, value in record.get("timings", {}).items():
            if isinstance(value, int | float):
                samples[str(name)].append(float(value))
        for event in record.get("trace", []):
            method = str(event.get("method", "unknown"))
            capability_counts[method] += 1
            capability_samples[method].append(float(event.get("duration_seconds", 0.0)))
        if not record.get("succeeded", False):
            program_failures += 1

    return {
        "requested_concurrency": concurrency,
        "effective_concurrency": worker_count,
        "attempted_requests": len(records),
        "request_failures": request_failures,
        "program_failures": program_failures,
        "warmup_failures": warmup_failures,
        "cleanup_failures": [error for error in cleanup_results if error is not None],
        "elapsed_seconds": elapsed,
        "throughput_requests_per_second": len(records) / elapsed if elapsed else 0.0,
        "successful_requests_per_second": (
            (len(records) - request_failures - program_failures) / elapsed if elapsed else 0.0
        ),
        "latency_seconds": {name: _summary(values) for name, values in sorted(samples.items())},
        "capabilities": {
            name: {
                "calls": capability_counts[name],
                "latency_seconds": _summary(values),
            }
            for name, values in sorted(capability_samples.items())
        },
    }


async def _main(args: argparse.Namespace) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {args.api_key}"} if args.api_key else {}
    timeout = httpx.Timeout(args.timeout_seconds)
    async with httpx.AsyncClient(
        base_url=args.base_url.rstrip("/"),
        headers=headers,
        timeout=timeout,
    ) as client:
        health_response = await client.get("/healthz")
        health_response.raise_for_status()
        health = health_response.json()
        levels = []
        for concurrency in args.concurrency:
            levels.append(
                await _run_level(
                    client,
                    concurrency=concurrency,
                    requests=args.requests,
                    warmup_per_worker=args.warmup_per_worker,
                    backend=args.backend,
                    code=args.code,
                    include_trace=args.include_trace,
                )
            )
    return {
        "base_url": args.base_url,
        "backend": args.backend,
        "health": health,
        "code_sha256": hashlib.sha256(args.code.encode("utf-8")).hexdigest(),
        "include_trace": args.include_trace,
        "requests_per_level": args.requests,
        "warmup_per_worker": args.warmup_per_worker,
        "levels": levels,
    }


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--api-key", default=os.getenv("OPENSAC_API_KEY", ""))
    parser.add_argument("--backend", choices=("local", "web"), default="local")
    parser.add_argument("--concurrency", type=_parse_concurrency, default=[1, 4, 8, 16])
    parser.add_argument("--requests", type=int, default=32)
    parser.add_argument("--warmup-per-worker", type=int, default=1)
    parser.add_argument("--timeout-seconds", type=float, default=600.0)
    parser.add_argument("--include-trace", action=argparse.BooleanOptionalAction, default=True)
    code_group = parser.add_mutually_exclusive_group()
    code_group.add_argument("--code", default="pass\n")
    code_group.add_argument("--code-file", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.requests < 1:
        parser.error("--requests must be at least 1")
    if args.warmup_per_worker < 0:
        parser.error("--warmup-per-worker must be non-negative")
    if args.code_file is not None:
        args.code = args.code_file.read_text(encoding="utf-8")
    return args


def main() -> None:
    args = _arguments()
    report = asyncio.run(_main(args))
    rendered = json.dumps(report, indent=2, sort_keys=True)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
