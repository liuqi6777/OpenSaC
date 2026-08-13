from __future__ import annotations

import subprocess

import typer
import uvicorn

from opensac.config import Settings
from opensac.sandbox import SANDBOX_CONTRACT

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
        [
            "docker",
            "build",
            "--build-arg",
            f"OPENSAC_SANDBOX_CONTRACT={SANDBOX_CONTRACT}",
            "-f",
            "sandbox/Dockerfile",
            "-t",
            settings.sandbox_image,
            ".",
        ],
        check=True,
    )
if __name__ == "__main__":
    app()
