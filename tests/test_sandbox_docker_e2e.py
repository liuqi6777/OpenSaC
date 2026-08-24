from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from opensac import OpenSAC, __version__

pytestmark = pytest.mark.skipif(
    os.environ.get("OPENSAC_DOCKER_E2E") != "1",
    reason="set OPENSAC_DOCKER_E2E=1 to build and exercise the container images",
)


@pytest.fixture(scope="module")
def sandbox_image() -> str:
    repo_root = Path(__file__).resolve().parents[1]
    image = f"opensac-sandbox-e2e:{os.getpid()}"
    environment = {**os.environ, "OPENSAC_SANDBOX_IMAGE": image}
    try:
        subprocess.run(
            [sys.executable, "-m", "opensac.cli", "build-sandbox"],
            cwd=repo_root,
            env=environment,
            check=True,
            timeout=600,
        )
        yield image
    finally:
        subprocess.run(
            ["docker", "image", "rm", "--force", image],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def test_built_image_exposes_contract_13_and_compact_sdk(sandbox_image: str) -> None:
    image = sandbox_image
    inspected = subprocess.run(
        [
            "docker",
            "image",
            "inspect",
            "--format",
            '{{ index .Config.Labels "org.opensac.sandbox.contract" }}',
            image,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert inspected.stdout.strip() == "13"

    inspected_version = subprocess.run(
        [
            "docker",
            "image",
            "inspect",
            "--format",
            '{{ index .Config.Labels "org.opencontainers.image.version" }}',
            image,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert inspected_version.stdout.strip() == __version__

    script = (
        "import importlib.util, json; "
        "from opensac_sdk import __version__; "
        "from opensac_sdk._resources import SearchResource; "
        "hit = {'source': 'doc_a', 'backend': 'local', 'rank': 1}; "
        "batch = {'query': 'q', 'hits': [hit], 'failure': None}; "
        "result = SearchResource(None).fuse_rrf([batch]); "
        "print(json.dumps({'version': __version__, "
        "'fusion': result, "
        "'types_module': importlib.util.find_spec('opensac_sdk.types') is not None, "
        "'models_module': importlib.util.find_spec('opensac_sdk.models') is not None, "
        "'search_module': importlib.util.find_spec('opensac_sdk.search') is not None}, "
        "sort_keys=True))"
    )
    executed = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "python",
            image,
            "-I",
            "-c",
            script,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(executed.stdout)
    assert payload["version"] == __version__
    assert payload["fusion"][0]["source"] == "doc_a"
    assert payload["fusion"][0]["fused_rank"] == 1
    assert payload["types_module"] is False
    assert payload["models_module"] is False
    assert payload["search_module"] is False


def test_compose_service_executes_a_sandbox_program(sandbox_image: str) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    service_image = f"opensac-service-e2e:{os.getpid()}"
    project = f"opensac-e2e-{os.getpid()}"
    data_dir = repo_root / f".opensac-service-e2e-{os.getpid()}"
    data_dir.mkdir()
    config_path = data_dir / "opensac.yaml"
    docker_host_platform = "darwin" if sys.platform == "darwin" else "linux"
    config_path.write_text(
        f"""
api:
  host: 0.0.0.0
storage:
  data_dir: {data_dir}
  broker_socket: {data_dir}/broker.sock
search:
  backend: web
sandbox:
  image: {sandbox_image}
  docker_host_platform: {docker_host_platform}
  experimental_persistent_interpreter: true
""",
        encoding="utf-8",
    )

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]

    docker_gid = 0 if sys.platform == "darwin" else Path("/var/run/docker.sock").stat().st_gid
    environment = {
        **os.environ,
        "OPENSAC_CONTAINER_DATA_DIR": str(data_dir),
        "OPENSAC_CONFIG_FILE": str(config_path),
        "OPENSAC_UID": str(os.getuid()),
        "OPENSAC_GID": str(os.getgid()),
        "OPENSAC_DOCKER_GID": str(docker_gid),
        "OPENSAC_ENV_FILE": "/dev/null",
        "OPENSAC_SERVICE_IMAGE": service_image,
        "OPENSAC_PORT": str(port),
    }
    compose = ["docker", "compose", "-p", project]

    try:
        inspected = subprocess.run(
            [*compose, "config", "--services"],
            cwd=repo_root,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        assert inspected.stdout.splitlines() == ["opensac"]

        subprocess.run(
            [
                "docker",
                "build",
                "--build-arg",
                f"OPENSAC_VERSION={__version__}",
                "--tag",
                service_image,
                ".",
            ],
            cwd=repo_root,
            env=environment,
            check=True,
            timeout=600,
        )

        subprocess.run(
            [*compose, "up", "--detach", "--no-build"],
            cwd=repo_root,
            env=environment,
            check=True,
            timeout=120,
        )

        deadline = time.monotonic() + 90
        while True:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/healthz", timeout=2
                ) as response:
                    assert response.status == 200
                break
            except OSError:
                if time.monotonic() >= deadline:
                    subprocess.run([*compose, "logs"], cwd=repo_root, env=environment)
                    raise
                time.sleep(1)

        with OpenSAC(base_url=f"http://127.0.0.1:{port}", timeout=60) as client:
            session_ids = []
            try:
                session = client.create_session()
                session_ids.append(session["id"])
                result = client.exec_code(session["id"], "print('compose-sandbox-ok')")

                repl = client.create_session(execution_mode="persistent_interpreter")
                session_ids.append(repl["id"])
                initialized = client.exec_code(
                    repl["id"],
                    "value = 41\n\ndef plus_one(number):\n    return number + 1",
                )
                reused = client.exec_code(repl["id"], "print(plus_one(value))")
                failed = client.exec_code(
                    repl["id"], "assigned_before_error = 7\nraise ValueError('ordinary')"
                )
                recovered = client.exec_code(repl["id"], "print(assigned_before_error)")
            finally:
                for session_id in session_ids:
                    client.delete_session(session_id)

        assert result["exit_code"] == 0
        assert result["stdout"] == "compose-sandbox-ok\n"
        assert result["succeeded"] is True
        assert initialized["interpreter_state"] == "ready"
        assert initialized["namespace_symbol_count"] >= 2
        assert reused["stdout"] == "42\n"
        assert reused["execution_mode"] == "persistent_interpreter"
        assert failed["exit_code"] == 1
        assert failed["interpreter_state"] == "ready"
        assert recovered["stdout"] == "7\n"
    finally:
        subprocess.run(
            [*compose, "down", "--remove-orphans"],
            cwd=repo_root,
            env=environment,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            ["docker", "image", "rm", "--force", service_image],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        shutil.rmtree(data_dir, ignore_errors=True)
