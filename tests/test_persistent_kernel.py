from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
KERNEL = ROOT / "sandbox" / "kernel.py"
RELAY = ROOT / "sandbox" / "relay.py"


async def _start_kernel(tmp_path: Path) -> tuple[asyncio.subprocess.Process, dict[str, str]]:
    digest = hashlib.sha256(str(tmp_path).encode()).hexdigest()[:16]
    socket_path = Path("/tmp") / f"opensac-kernel-test-{digest}.sock"
    socket_path.unlink(missing_ok=True)
    environment = {
        **os.environ,
        "OPENSAC_KERNEL_SOCKET": str(socket_path),
        "OPENSAC_KERNEL_WORKSPACE_ROOT": str(tmp_path),
    }
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        str(KERNEL),
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    for _ in range(200):
        if socket_path.exists():
            return process, environment
        if process.returncode is not None:
            stdout, stderr = await process.communicate()
            raise AssertionError(f"kernel exited early: {stdout.decode()} {stderr.decode()}")
        await asyncio.sleep(0.01)
    process.kill()
    await process.wait()
    raise AssertionError("kernel socket did not appear")


async def _run_cell(
    tmp_path: Path,
    environment: dict[str, str],
    sequence: int,
    code: str,
) -> tuple[int, str, str, dict[str, object]]:
    program = tmp_path / f"program-{sequence}.py"
    output = tmp_path / f"output-{sequence}.json"
    ready = tmp_path / f"ready-{sequence}"
    result = tmp_path / f"kernel-result-{sequence}.json"
    program.write_text(code, encoding="utf-8")
    relay_environment = {
        **environment,
        "OPENSAC_SESSION_TOKEN": "session-token",
        "OPENSAC_EXECUTION_ID": f"execution-{sequence}",
        "OPENSAC_OUTPUT_PATH": str(output),
        "OPENSAC_READY_PATH": str(ready),
        "OPENSAC_WORKSPACE": str(tmp_path),
        "OPENSAC_KERNEL_RESULT_PATH": str(result),
    }
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        str(RELAY),
        str(program),
        env=relay_environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    metadata = json.loads(result.read_text(encoding="utf-8"))
    return process.returncode or 0, stdout.decode(), stderr.decode(), metadata


async def test_kernel_preserves_globals_and_partial_state_after_exception(
    tmp_path: Path,
) -> None:
    kernel, environment = await _start_kernel(tmp_path)
    try:
        first = await _run_cell(
            tmp_path,
            environment,
            1,
            "base = 40\ndef add_two(value):\n    return value + 2\nprint('defined')",
        )
        failed = await _run_cell(
            tmp_path,
            environment,
            2,
            "partial = add_two(base)\nraise ValueError('expected')",
        )
        recovered = await _run_cell(
            tmp_path,
            environment,
            3,
            "print(partial)",
        )
    finally:
        if kernel.returncode is None:
            kernel.terminate()
        await kernel.communicate()
        Path(environment["OPENSAC_KERNEL_SOCKET"]).unlink(missing_ok=True)

    assert first[0] == 0
    assert first[1] == "defined\n"
    assert first[3]["namespace_symbol_count"] == 2
    assert failed[0] == 1
    assert "ValueError: expected" in failed[2]
    assert recovered[0] == 0
    assert recovered[1] == "42\n"
    assert recovered[3]["interpreter_loss_reason"] is None


async def test_kernel_reports_background_thread_as_lost(tmp_path: Path) -> None:
    kernel, environment = await _start_kernel(tmp_path)
    result = await _run_cell(
        tmp_path,
        environment,
        1,
        "import threading, time\nthreading.Thread(target=lambda: time.sleep(30)).start()",
    )
    await asyncio.wait_for(kernel.wait(), timeout=2)
    Path(environment["OPENSAC_KERNEL_SOCKET"]).unlink(missing_ok=True)

    assert result[0] == 0
    assert result[3]["interpreter_loss_reason"] == "background_threads"
    assert kernel.returncode == 70
