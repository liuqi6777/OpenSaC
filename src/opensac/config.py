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
    serper_api_key: str = ""
    # Concurrent document fetches inside one `content.*` call. The broker's own
    # semaphore admits a whole call as one unit, so without this a program
    # asking for fifty pages opens fifty simultaneous requests to the provider,
    # which is a rate limit on the web backend and a thundering herd on the
    # local one.
    backend_fetch_concurrency: int = Field(default=6, ge=1, le=64)
    # Document text cached per session, in bytes. `grep` and `read` are meant to
    # be used repeatedly over one candidate pool, so without a cache the
    # recommended survey/locate/verify shape refetches the whole pool once per
    # stage -- affordable against a local index, three times the bill and the
    # latency against a paid scrape API. Beyond the budget new documents are
    # simply not cached; nothing is evicted, so a long rollout degrades to the
    # old behaviour instead of thrashing.
    session_content_cache_bytes: int = Field(default=32_000_000, ge=0)

    sandbox_image: str = "opensac-sandbox:latest"
    sandbox_timeout_seconds: int = 120
    sandbox_memory: str = "512m"
    sandbox_cpus: float = 1.0
    sandbox_pids_limit: int = 64
    # Ceiling on concurrently running containers. An external harness driving
    # /exec across many parallel rollouts would otherwise start one container
    # per in-flight tool call and exhaust the host.
    sandbox_max_concurrency: int = 8

    max_turns: int = 8
    max_search_calls: int = 200
    max_llm_calls: int = 30
    max_concurrency: int = 12
    max_output_bytes: int = Field(default=1_000_000, ge=1024)
    # Only reached by a session that disables context decoupling, where every
    # capability result is echoed back through the trace. Generous on purpose:
    # that arm exists to measure what the results cost in context, so the cap is
    # a guard against one runaway page breaking the RPC response, not a budget.
    max_context_payload_bytes: int = Field(default=200_000, ge=1024)
