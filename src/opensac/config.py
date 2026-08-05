from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OPENSAC_", env_file=".env", extra="ignore")

    data_dir: Path = Path(".opensac")
    broker_socket: Path = Path(".opensac/broker.sock")
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    api_key: str = ""

    model_api_key: str = ""
    model_base_url: str | None = None
    model_name: str = ""
    model_temperature: float = 0.1

    local_search_base_url: str = "http://127.0.0.1:8081"
    perplexity_api_key: str = ""

    sandbox_image: str = "opensac-sandbox:latest"
    sandbox_timeout_seconds: int = 120
    sandbox_memory: str = "512m"
    sandbox_cpus: float = 1.0
    sandbox_pids_limit: int = 64

    max_turns: int = 8
    max_search_calls: int = 200
    max_llm_calls: int = 30
    max_concurrency: int = 12
    max_output_bytes: int = Field(default=1_000_000, ge=1024)
