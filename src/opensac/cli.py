from __future__ import annotations

import subprocess

import typer
import uvicorn

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
if __name__ == "__main__":
    app()
