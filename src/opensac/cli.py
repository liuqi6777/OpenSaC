from __future__ import annotations

import json
import subprocess

import typer
import uvicorn

from opensac.client import OpenSAC
from opensac.config import Settings

app = typer.Typer(no_args_is_help=True)


@app.command()
def serve() -> None:
    """Start the public OpenSAC API and capability broker."""
    settings = Settings()
    uvicorn.run(
        "opensac.api.app:create_app",
        factory=True,
        host=settings.api_host,
        port=settings.api_port,
    )


@app.command("build-sandbox")
def build_sandbox() -> None:
    """Build the hardened sandbox image with Docker."""
    settings = Settings()
    subprocess.run(
        ["docker", "build", "-f", "sandbox/Dockerfile", "-t", settings.sandbox_image, "."],
        check=True,
    )


@app.command()
def run(
    task: str,
    base_url: str = "http://127.0.0.1:8000",
    api_key: str = "",
    backends: str = "web,local",
) -> None:
    """Submit one task to a running OpenSAC API."""
    with OpenSAC(base_url=base_url, api_key=api_key) as client:
        session = client.create_session(backends=backends.split(","))
        result = client.create_and_wait(session["id"], task)
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    app()
