from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("OPENSAC_DOCKER_E2E") != "1",
    reason="set OPENSAC_DOCKER_E2E=1 to build and exercise the sandbox image",
)


def test_built_image_exposes_contract_3_and_local_rrf() -> None:
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
        assert inspected.stdout.strip() == "3"

        script = (
            "import json; "
            "from opensac_sdk import SearchBatch, SearchHit, sdk; "
            "hit = SearchHit(ref='ref_a', backend='local', rank=1); "
            "result = sdk.search.fuse_rrf([SearchBatch(query='q', hits=[hit])]); "
            "print(json.dumps(result.model_dump(mode='json'), sort_keys=True))"
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
        assert payload["candidates"][0]["ref"] == "ref_a"
        assert payload["candidates"][0]["fused_rank"] == 1
    finally:
        subprocess.run(
            ["docker", "image", "rm", "--force", image],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
