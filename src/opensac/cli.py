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
    wheels_dir = "_sandbox_wheel"
    subprocess.run(
        [
            "pip", "wheel", "--no-deps", "--no-build-isolation", 
            "--ignore-requires-python", "-w", wheels_dir, "packages/opensac-sdk"
        ],
        check=True,
    )
    subprocess.run(
        [
            "pip", "download", "-d", wheels_dir,
            "httpx>=0.28", "pydantic>=2.11",
            "--python-version", "3.12", "--only-binary=:all:",
            "--platform", "manylinux2014_x86_64", "--platform", "manylinux_2_17_x86_64",
            "--platform", "linux_x86_64", "--implementation", "cp", "--abi", "cp312",
        ],
        check=True,
    )
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
