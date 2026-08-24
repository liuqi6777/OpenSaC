from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Annotated

import typer
import uvicorn

from opensac import __version__
from opensac._optional import MissingOptionalDependency
from opensac.api.app import create_app
from opensac.config import ConfigurationError, Settings, load_settings
from opensac.sandbox import SANDBOX_CONTRACT

app = typer.Typer(no_args_is_help=True)


@app.command()
def serve(
    config: Annotated[
        Path | None,
        typer.Option(help="Nested YAML service configuration file."),
    ] = None,
) -> None:
    """Start the public OpenSAC API and capability broker."""
    settings = _load_cli_settings(config)
    uvicorn.run(
        create_app(settings),
        host=settings.api_host,
        port=settings.api_port,
    )


@app.command("mcp")
def serve_mcp() -> None:
    """Start the local OpenSAC MCP server over stdio."""
    from opensac.agent.mcp import run

    try:
        run()
    except MissingOptionalDependency as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None


@app.command("agent-run")
def agent_run(
    source: Annotated[
        str,
        typer.Argument(help="Python source file, or '-' to read the program from stdin."),
    ] = "-",
) -> None:
    """Run a program in the current agent conversation's leased session."""
    from opensac.agent.cli import run_command

    run_command(source)


@app.command("build-sandbox")
def build_sandbox(
    config: Annotated[
        Path | None,
        typer.Option(help="Nested YAML service configuration file."),
    ] = None,
    network: Annotated[
        str | None,
        typer.Option(help="Docker build network mode, for example 'host'."),
    ] = None,
    pip_index_url: Annotated[
        str | None,
        typer.Option(help="Custom pip index URL used inside the build."),
    ] = None,
    pip_trusted_host: Annotated[
        str | None,
        typer.Option(help="Trusted host for the custom pip index URL."),
    ] = None,
) -> None:
    """Build the hardened sandbox image with Docker."""
    settings = _load_cli_settings(config)
    command = ["docker", "build"]
    if network is not None:
        command.extend(["--network", network])
    command.extend(
        [
            "--build-arg",
            f"OPENSAC_SANDBOX_CONTRACT={SANDBOX_CONTRACT}",
            "--build-arg",
            f"OPENSAC_VERSION={__version__}",
        ]
    )
    if pip_index_url is not None:
        command.extend(["--build-arg", f"PIP_INDEX_URL={pip_index_url}"])
    if pip_trusted_host is not None:
        command.extend(["--build-arg", f"PIP_TRUSTED_HOST={pip_trusted_host}"])
    command.extend(
        [
            "-f",
            "sandbox/Dockerfile",
            "-t",
            settings.sandbox_image,
            ".",
        ]
    )
    subprocess.run(
        command,
        check=True,
    )


def _load_cli_settings(config: Path | None) -> Settings:
    try:
        return load_settings(config)
    except ConfigurationError as exc:
        raise typer.BadParameter(str(exc), param_hint="--config") from exc


if __name__ == "__main__":
    app()
