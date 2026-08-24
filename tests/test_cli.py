from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

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
    monkeypatch.setattr(cli, "load_settings", lambda _: cli.Settings())

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
    monkeypatch.setattr(cli, "load_settings", lambda _: cli.Settings())

    cli.build_sandbox(network="host")

    assert calls[0][:4] == ["docker", "build", "--network", "host"]


def test_serve_loads_configuration_once(monkeypatch, tmp_path: Path) -> None:
    config = tmp_path / "opensac.yaml"
    settings = cli.Settings(api_host="0.0.0.0", api_port=9000)
    loaded: list[Path | None] = []
    app_instance = object()
    calls: list[tuple[object, str, int]] = []

    def fake_load(config_path: Path | None) -> cli.Settings:
        loaded.append(config_path)
        return settings

    monkeypatch.setattr(cli, "load_settings", fake_load)
    monkeypatch.setattr(cli, "create_app", lambda received: app_instance)
    monkeypatch.setattr(
        cli.uvicorn,
        "run",
        lambda app, *, host, port: calls.append((app, host, port)),
    )

    cli.serve(config)

    assert loaded == [config]
    assert calls == [(app_instance, "0.0.0.0", 9000)]


def test_serve_prints_local_dashboard_url(monkeypatch, capsys) -> None:
    settings = cli.Settings(api_host="127.0.0.1", api_port=8123)
    monkeypatch.setattr(cli, "load_settings", lambda _: settings)
    monkeypatch.setattr(cli, "create_app", lambda _: object())
    monkeypatch.setattr(cli.uvicorn, "run", lambda *args, **kwargs: None)

    cli.serve()

    assert "Dashboard: http://127.0.0.1:8123/dashboard" in capsys.readouterr().out


def test_build_sandbox_uses_image_from_yaml(monkeypatch, tmp_path: Path) -> None:
    config = tmp_path / "opensac.yaml"
    config.write_text("sandbox:\n  image: example/opensac-sandbox:test\n", encoding="utf-8")
    calls: list[list[str]] = []

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, *, check: calls.append(command),
    )

    cli.build_sandbox(config=config)

    assert calls[0][calls[0].index("-t") + 1] == "example/opensac-sandbox:test"


def test_cli_configuration_error_exits_nonzero(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(cli.app, ["serve", "--config", "missing.yaml"])

    assert result.exit_code == 2
    assert "missing.yaml" in result.output


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


def test_service_dockerfile_copies_dashboard_before_building_wheel() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    dockerfile = (repo_root / "Dockerfile").read_text()

    dashboard_copy = dockerfile.index("COPY dashboard ./dashboard")
    wheel_build = dockerfile.index("RUN uv build --all-packages --wheel")
    assert dashboard_copy < wheel_build
