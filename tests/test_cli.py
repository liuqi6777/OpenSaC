from __future__ import annotations

import subprocess
from pathlib import Path

from opensac import cli


def test_build_sandbox_builds_directly_from_source(monkeypatch) -> None:
    calls: list[tuple[list[str], bool]] = []

    def fake_run(command: list[str], *, check: bool) -> None:
        calls.append((command, check))

    monkeypatch.setattr(subprocess, "run", fake_run)

    cli.build_sandbox()

    assert calls == [
        (
            [
                "docker",
                "build",
                "--build-arg",
                f"OPENSAC_SANDBOX_CONTRACT={cli.SANDBOX_CONTRACT}",
                "-f",
                "sandbox/Dockerfile",
                "-t",
                cli.Settings().sandbox_image,
                ".",
            ],
            True,
        )
    ]


def test_sandbox_dockerfile_installs_sdk_from_source() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    dockerfile = (repo_root / "sandbox" / "Dockerfile").read_text()

    assert "COPY packages/opensac-sdk /opt/opensac-sdk" in dockerfile
    assert "RUN pip install --no-cache-dir /opt/opensac-sdk" in dockerfile
    assert "_sandbox_wheel" not in dockerfile
