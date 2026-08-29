"""Run an opensac_sdk program without a control model.

The control model normally writes these programs and the API server runs them.
This runner stands up just the broker (and optionally the Docker sandbox) so you
can iterate on SDK code directly.

    python examples/run_sdk_locally.py examples/research_pipeline.py
    python examples/run_sdk_locally.py my_program.py --docker
    python examples/run_sdk_locally.py my_program.py --config configs/web.yaml

Host mode is fast but runs the program in this process, so the sandbox code
validator and the container isolation do not apply. Use --docker to exercise the
real execution path.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import runpy
import secrets
import tempfile
from pathlib import Path

from opensac.backends.document.jina import JinaReaderBackend
from opensac.backends.document.local_http import LocalDocumentBackend
from opensac.backends.search.local_http import LocalSearchBackend
from opensac.backends.search.serper import SerperBackend
from opensac.broker import BrokerRuntime, BrokerService, RetrievalRoute
from opensac.config import Settings, load_settings
from opensac.models import Session
from opensac.sandbox import DockerSandbox, UnsafeCodeError
from opensac.sandbox.base import SandboxRequest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("program", type=Path, help="Python file that imports opensac_sdk")
    parser.add_argument("--docker", action="store_true", help="run inside the real sandbox image")
    parser.add_argument("--workspace", type=Path, default=None, help="reuse a workspace directory")
    parser.add_argument("--config", type=Path, default=None, help="OpenSAC YAML configuration")
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    settings = load_settings(args.config)
    backend = settings.backend_name
    workspace = args.workspace or Path(tempfile.mkdtemp(prefix="opensac-sdk-"))
    workspace.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(16)

    if backend == "local":
        search_base_url = settings.backends.search.base_url
        document_base_url = settings.backends.document.base_url
        assert search_base_url is not None
        assert document_base_url is not None
        search_backend = LocalSearchBackend(search_base_url)
        document_backend = LocalDocumentBackend(document_base_url)
    else:
        search_backend = SerperBackend(settings.serper_api_key)
        document_backend = JinaReaderBackend(settings.jina_api_key)
    service = BrokerService(
        {backend: RetrievalRoute(search=search_backend, document=document_backend)},
        llm_backend=None,
        max_concurrency=settings.max_concurrency,
    )
    service.register_session(
        Session(
            id="sdk-runner",
            token=token,
            backends=[backend],
            workspace=str(workspace),
        )
    )
    runtime = BrokerRuntime(service, workspace / "broker.sock")
    await runtime.start()
    print(f"broker    {runtime.socket_path}")
    print(f"workspace {workspace}")
    print(f"backend   {backend}\n")

    try:
        if args.docker:
            exit_code = await run_in_docker(args.program, workspace, token, runtime, settings)
        else:
            exit_code = await run_on_host(args.program, workspace, token, runtime)
    finally:
        await runtime.stop()

    usage = service.sessions[token].policy.usage
    print(f"\nusage     search_calls={usage.search_calls} llm_calls={usage.llm_calls}")
    print(f"files     {sorted(p.name for p in workspace.iterdir())}")
    return exit_code


async def run_on_host(
    program: Path,
    workspace: Path,
    token: str,
    runtime: BrokerRuntime,
) -> int:
    os.environ["OPENSAC_BROKER_SOCKET"] = str(runtime.socket_path)
    os.environ["OPENSAC_SESSION_TOKEN"] = token
    os.environ["OPENSAC_WORKSPACE"] = str(workspace)
    os.environ["OPENSAC_OUTPUT_PATH"] = str(workspace / ".opensac-output.json")
    # The SDK is synchronous and the broker shares this event loop, so the
    # program must run in a worker thread or the two will deadlock.
    try:
        await asyncio.to_thread(runpy.run_path, str(program), run_name="__main__")
    except Exception as exc:  # noqa: BLE001 - surface program failures verbatim
        print(f"\nprogram raised {type(exc).__name__}: {exc}")
        return 1
    return 0


async def run_in_docker(
    program: Path,
    workspace: Path,
    token: str,
    runtime: BrokerRuntime,
    settings: Settings,
) -> int:
    sandbox = DockerSandbox(
        image=settings.sandbox_image,
        broker_socket=runtime.socket_path,
        timeout_seconds=settings.sandbox_timeout_seconds,
        memory=settings.sandbox_memory,
        cpus=settings.sandbox_cpus,
        pids_limit=settings.sandbox_pids_limit,
        max_output_bytes=settings.max_output_bytes,
    )
    try:
        result = await sandbox.execute(
            SandboxRequest(
                code=program.read_text(encoding="utf-8"),
                workspace=workspace,
                session_token=token,
            )
        )
    except UnsafeCodeError as exc:
        # The sandbox validator rejects the program before Docker is invoked.
        print(f"rejected by the sandbox code validator: {exc}")
        return 1
    print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="")
    print(
        f"\nexit_code {result.exit_code} timed_out={result.timed_out} "
        f"duration={result.duration_seconds:.1f}s"
    )
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
