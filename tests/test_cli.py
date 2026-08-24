from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import typer

from opensac import cli
from opensac._optional import MissingOptionalDependency
from opensac.agent import mcp


def test_mcp_command_starts_stdio_server(monkeypatch) -> None:
    called = False

    def fake_run() -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(mcp, "run", fake_run)

    cli.serve_mcp()

    assert called


def test_mcp_command_reports_missing_extra(monkeypatch, capsys) -> None:
    def unavailable() -> None:
        raise MissingOptionalDependency(
            "MCP support requires optional dependencies (mcp); install with 'opensac[mcp]'."
        )

    monkeypatch.setattr(mcp, "run", unavailable)

    with pytest.raises(typer.Exit) as raised:
        cli.serve_mcp()

    assert raised.value.exit_code == 1
    assert "opensac[mcp]" in capsys.readouterr().err


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
                "--build-arg",
                f"OPENSAC_VERSION={cli.__version__}",
                "-f",
                "sandbox/Dockerfile",
                "-t",
                cli.Settings().sandbox_image,
                ".",
            ],
            True,
        )
    ]


def test_build_sandbox_accepts_network_mode(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], *, check: bool) -> None:
        assert check is True
        calls.append(command)

    monkeypatch.setattr(subprocess, "run", fake_run)

    cli.build_sandbox(network="host")

    assert calls[0][:4] == ["docker", "build", "--network", "host"]


def test_sandbox_dockerfile_installs_sdk_from_source() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    dockerfile = (repo_root / "sandbox" / "Dockerfile").read_text()

    assert "COPY packages/opensac-sdk /opt/opensac-sdk" in dockerfile
    assert "RUN pip install --no-cache-dir /opt/opensac-sdk" in dockerfile
    assert "org.opencontainers.image.version=$OPENSAC_VERSION" in dockerfile
    assert "_sandbox_wheel" not in dockerfile


def test_service_dockerfile_installs_pipeline_llm_profile() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    dockerfile = (repo_root / "Dockerfile").read_text()

    assert 'python -m pip install --no-cache-dir "$1[llm]"' in dockerfile
