from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OPENSAC_", env_file=".env", extra="ignore")

    data_dir: Path = Path(".opensac")
    broker_socket: Path = Path(".opensac/broker.sock")
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    api_key: str = ""
    # Stable across restarts for scheduler affinity.  Empty derives a stable
    # value from hostname and data_dir; every process still gets a fresh epoch.
    worker_id: str = ""
    build_commit: str = ""
    sandbox_image_digest: str = ""
    backend_revision: str = ""
    backend_metadata_hash: str = ""
    max_active_sessions: int = Field(default=0, ge=0)
    # Zero preserves the original explicit-lifecycle behaviour. Deployments
    # serving ephemeral rollout workers can opt into lease-style cleanup; the
    # reaper never removes a session with an execution holding its session lock.
    session_ttl_seconds: float = Field(default=0.0, ge=0.0)
    session_reaper_interval_seconds: float = Field(default=60.0, gt=0.0)
    session_tombstone_ttl_seconds: float = Field(default=86_400.0, ge=0.0)

    model_api_key: str = ""
    model_base_url: str | None = None
    model_name: str = ""

    # One deployment-wide search backend. Sessions inherit this value so a
    # caller cannot switch corpora or credentials through the public API.
    search_backend: Literal["local", "web"] = "local"
    local_search_base_url: str = "http://127.0.0.1:8081"
    serper_api_key: str = ""
    jina_api_key: str = ""
    # Admission limits are enforced by the broker before query fan-out. Keep
    # the defaults wide enough for research pipelines while bounding one
    # malformed/generated call independently of rollout-level usage metrics.
    search_max_queries_per_request: int = Field(default=64, ge=1)
    search_max_query_chars: int = Field(default=4096, ge=1)
    # Retrieval depth (offset + limit), matching the local backend's top_k.
    search_max_top_k: int = Field(default=600, ge=1)
    # Structured extraction limits are enforced before the provider is called.
    # Byte limits use UTF-8 encoded request sizes so non-ASCII inputs cannot
    # exceed the admission budget while appearing short in Python characters.
    extract_max_items: int = Field(default=256, ge=1)
    extract_max_instruction_bytes: int = Field(default=16_384, ge=1)
    extract_max_schema_bytes: int = Field(default=65_536, ge=1)
    extract_max_item_bytes: int = Field(default=65_536, ge=1)
    extract_max_total_item_bytes: int = Field(default=2_097_152, ge=1)
    extract_max_schema_depth: int = Field(default=8, ge=1)
    extract_max_repair_attempts: int = Field(default=1, ge=0, le=1)
    # Evidence locators are only minted for passages small enough to be
    # returned and validated as one citation payload.
    citation_max_evidence_chars: int = Field(default=16_000, ge=1)
    citation_max_evidence_records: int = Field(default=4_096, ge=1)
    citation_max_evidence_passage_bytes: int = Field(default=33_554_432, ge=1)
    content_max_refs_per_request: int = Field(default=256, ge=1)
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

    # Provider reliability is deployment policy, never a knob generated code
    # can tune per call. `none` freezes the 0.2 baseline at one attempt; `safe`
    # enables the bounded transient retry profile below.
    provider_retry_profile: Literal["none", "safe"] = "none"
    provider_max_attempts: int = Field(default=3, ge=1, le=3)
    provider_attempt_timeout_seconds: float = Field(default=30.0, gt=0.0)
    provider_logical_deadline_seconds: float = Field(default=90.0, gt=0.0)
    provider_base_backoff_seconds: float = Field(default=0.5, ge=0.0)
    provider_max_backoff_seconds: float = Field(default=4.0, ge=0.0)
    provider_max_total_backoff_seconds: float = Field(default=15.0, ge=0.0)
    provider_max_retry_after_seconds: float = Field(default=15.0, ge=0.0)
    # JSON dictionaries keyed by local.search/local.document/web.search/web.scrape.
    # Empty RPS/burst maps disable request-rate limiting. Concurrency falls back
    # to max_concurrency for search and backend_fetch_concurrency for documents.
    provider_operation_concurrency: dict[str, int] = Field(default_factory=dict)
    provider_operation_requests_per_second: dict[str, float] = Field(default_factory=dict)
    provider_operation_burst: dict[str, int] = Field(default_factory=dict)

    # 0.3.1 in-flight sharing. Disabled by default so upgrading cannot alter a
    # frozen baseline's latency or failure timing.
    provider_inflight_coalescing: bool = False
    provider_max_inflight_keys: int = Field(default=256, ge=1)
    provider_max_waiters_per_key: int = Field(default=64, ge=1)

    @model_validator(mode="after")
    def validate_provider_operation_maps(self) -> Settings:
        allowed = {
            "local.search",
            "local.document",
            "web.search",
            "web.scrape",
        }
        mappings = {
            "provider_operation_concurrency": self.provider_operation_concurrency,
            "provider_operation_requests_per_second": (
                self.provider_operation_requests_per_second
            ),
            "provider_operation_burst": self.provider_operation_burst,
        }
        for name, values in mappings.items():
            unknown = set(values) - allowed
            if unknown:
                raise ValueError(f"{name} has unknown operations: {sorted(unknown)}")
        missing_rates = set(self.provider_operation_burst) - set(
            self.provider_operation_requests_per_second
        )
        if missing_rates:
            raise ValueError(
                "provider_operation_burst requires requests_per_second for: "
                f"{sorted(missing_rates)}"
            )
        return self

    sandbox_image: str = "opensac-sandbox:latest"
    # `cold` preserves one docker run per execution. `warm` keeps one hardened
    # container per active session while still starting a fresh Python process
    # for every program.
    sandbox_mode: Literal["cold", "warm"] = "cold"
    sandbox_warm_idle_seconds: float = Field(default=300.0, ge=0.0)
    # Zero preserves the previous unbounded warm registry.  RL deployments set
    # this below max_active_sessions and let idle namespaces be recreated.
    sandbox_max_warm_containers: int = Field(default=0, ge=0)
    sandbox_timeout_seconds: int = 120
    sandbox_memory: str = "512m"
    sandbox_cpus: float = 1.0
    sandbox_pids_limit: int = 64
    # Ceiling on concurrently running containers. An external harness driving
    # /exec across many parallel rollouts would otherwise start one container
    # per in-flight tool call and exhaust the host.
    sandbox_max_concurrency: int = 8

    max_concurrency: int = 12
    max_output_bytes: int = Field(default=1_000_000, ge=1024)
    # Only reached by a session that disables context decoupling, where every
    # capability result is echoed back through the trace. Generous on purpose:
    # that arm exists to measure what the results cost in context, so the cap is
    # a guard against one runaway page breaking the RPC response, not a budget.
    max_context_payload_bytes: int = Field(default=200_000, ge=1024)
