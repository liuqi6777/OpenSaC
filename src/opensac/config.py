from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from ipaddress import ip_address
from pathlib import Path
from typing import Any, Literal

import yaml
from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from opensac._version import __version__

DEFAULT_SANDBOX_IMAGE = f"ghcr.io/liuqi6777/opensac-sandbox:{__version__}"
DEFAULT_LOCAL_BACKEND_BASE_URL = "http://127.0.0.1:8081"

_SECRET_ENV_FIELDS = {
    "OPENSAC_API_KEY": "api_key",
    "OPENSAC_MODEL_API_KEY": "model_api_key",
    "OPENSAC_SERPER_API_KEY": "serper_api_key",
    "OPENSAC_JINA_API_KEY": "jina_api_key",
}

_YAML_FIELDS = {
    "api": {"host": "api_host", "port": "api_port"},
    "dashboard": {"enabled": "dashboard_enabled"},
    "storage": {"data_dir": "data_dir", "broker_socket": "broker_socket"},
    "deployment": {
        "worker_id": "worker_id",
        "build_commit": "build_commit",
        "sandbox_image_digest": "sandbox_image_digest",
        "backend_revision": "backend_revision",
        "backend_metadata_hash": "backend_metadata_hash",
    },
    "sessions": {
        "max_active": "max_active_sessions",
        "ttl_seconds": "session_ttl_seconds",
        "reaper_interval_seconds": "session_reaper_interval_seconds",
        "tombstone_ttl_seconds": "session_tombstone_ttl_seconds",
        "content_cache_bytes": "session_content_cache_bytes",
    },
    "providers": {
        "fetch_concurrency": "backend_fetch_concurrency",
        "retry_profile": "provider_retry_profile",
        "max_attempts": "provider_max_attempts",
        "attempt_timeout_seconds": "provider_attempt_timeout_seconds",
        "logical_deadline_seconds": "provider_logical_deadline_seconds",
        "base_backoff_seconds": "provider_base_backoff_seconds",
        "max_backoff_seconds": "provider_max_backoff_seconds",
        "max_total_backoff_seconds": "provider_max_total_backoff_seconds",
        "max_retry_after_seconds": "provider_max_retry_after_seconds",
        "services": "provider_services",
        "inflight_coalescing": "provider_inflight_coalescing",
        "max_inflight_keys": "provider_max_inflight_keys",
        "max_waiters_per_key": "provider_max_waiters_per_key",
        "result_cache_ttl_seconds": "provider_result_cache_ttl_seconds",
        "result_cache_max_bytes": "provider_result_cache_max_bytes",
    },
    "sandbox": {
        "image": "sandbox_image",
        "docker_host_platform": "sandbox_docker_host_platform",
        "mode": "sandbox_mode",
        "experimental_persistent_interpreter": "experimental_persistent_interpreter",
        "warm_idle_seconds": "sandbox_warm_idle_seconds",
        "max_warm_containers": "sandbox_max_warm_containers",
        "timeout_seconds": "sandbox_timeout_seconds",
        "memory": "sandbox_memory",
        "cpus": "sandbox_cpus",
        "pids_limit": "sandbox_pids_limit",
        "max_concurrency": "sandbox_max_concurrency",
    },
    "limits": {
        "max_concurrency": "max_concurrency",
        "max_output_bytes": "max_output_bytes",
        "max_context_payload_bytes": "max_context_payload_bytes",
    },
}

_BACKEND_YAML_FIELDS = {
    "search": {"provider", "base_url"},
    "document": {"provider", "base_url"},
    "rerank": {"provider", "model"},
    "llm": {"provider", "model", "base_url"},
}

_CAPABILITY_YAML_FIELDS = {
    "search": {"max_queries_per_request", "max_query_chars", "max_top_k"},
    "content": {
        "max_sources_per_request",
        "url_admission",
        "batch_deadline_seconds",
    },
    "extraction": {
        "max_items",
        "max_instruction_bytes",
        "max_schema_bytes",
        "max_item_bytes",
        "max_total_item_bytes",
        "max_schema_depth",
        "max_repair_attempts",
    },
}

_NESTED_YAML_FIELDS = {
    "backends": _BACKEND_YAML_FIELDS,
    "capabilities": _CAPABILITY_YAML_FIELDS,
}

_SECRET_YAML_FIELDS = {
    ("api", "key"): "OPENSAC_API_KEY",
    ("api", "api_key"): "OPENSAC_API_KEY",
    ("providers", "serper_api_key"): "OPENSAC_SERPER_API_KEY",
    ("providers", "jina_api_key"): "OPENSAC_JINA_API_KEY",
}

_SECRET_BACKEND_YAML_FIELDS = {
    ("search", "key"): "OPENSAC_SERPER_API_KEY",
    ("search", "api_key"): "OPENSAC_SERPER_API_KEY",
    ("document", "key"): "OPENSAC_JINA_API_KEY",
    ("document", "api_key"): "OPENSAC_JINA_API_KEY",
    ("rerank", "key"): "OPENSAC_JINA_API_KEY",
    ("rerank", "api_key"): "OPENSAC_JINA_API_KEY",
    ("llm", "key"): "OPENSAC_MODEL_API_KEY",
    ("llm", "api_key"): "OPENSAC_MODEL_API_KEY",
}


class ConfigurationError(ValueError):
    """Raised when deployment configuration cannot be loaded safely."""


class SearchBackendSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["local", "serper"] = "local"
    base_url: str | None = None

    @model_validator(mode="after")
    def validate_connection(self) -> SearchBackendSettings:
        if self.provider == "local":
            self.base_url = self.base_url or DEFAULT_LOCAL_BACKEND_BASE_URL
        elif self.base_url is not None:
            raise ValueError("backends.search.base_url is supported only by the local provider")
        return self


class DocumentBackendSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["local", "jina"] = "local"
    base_url: str | None = None

    @model_validator(mode="after")
    def validate_connection(self) -> DocumentBackendSettings:
        if self.provider == "local":
            self.base_url = self.base_url or DEFAULT_LOCAL_BACKEND_BASE_URL
        elif self.base_url is not None:
            raise ValueError("backends.document.base_url is supported only by the local provider")
        return self


class RerankBackendSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["lexical", "jina"] = "lexical"
    model: str = ""

    @model_validator(mode="after")
    def validate_provider(self) -> RerankBackendSettings:
        if self.provider == "jina" and not self.model.strip():
            raise ValueError("backends.rerank.model is required for the jina provider")
        if self.provider == "lexical" and self.model.strip():
            raise ValueError("backends.rerank.model is supported only by the jina provider")
        return self


class LLMBackendSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["none", "openai_compatible"] = "none"
    model: str = ""
    base_url: str | None = None

    @model_validator(mode="after")
    def validate_provider(self) -> LLMBackendSettings:
        if self.provider == "openai_compatible" and not self.model.strip():
            raise ValueError("backends.llm.model is required for the openai_compatible provider")
        if self.provider == "none" and (self.model or self.base_url is not None):
            raise ValueError("backends.llm.model and base_url require an enabled LLM provider")
        return self


class BackendSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    search: SearchBackendSettings = Field(default_factory=SearchBackendSettings)
    document: DocumentBackendSettings = Field(default_factory=DocumentBackendSettings)
    rerank: RerankBackendSettings = Field(default_factory=RerankBackendSettings)
    llm: LLMBackendSettings = Field(default_factory=LLMBackendSettings)


class SearchCapabilitySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Admission limits are enforced by the broker before query fan-out. Keep
    # the defaults wide enough for research pipelines while bounding one
    # malformed/generated call independently of rollout-level usage metrics.
    max_queries_per_request: int = Field(default=64, ge=1)
    max_query_chars: int = Field(default=4096, ge=1)
    # Retrieval depth (offset + limit), matching the local backend's top_k.
    max_top_k: int = Field(default=600, ge=1)


class ContentCapabilitySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_sources_per_request: int = Field(default=256, ge=1)
    # Web deployments accept bounded public HTTP(S) URLs directly so a URL
    # selected from an earlier agent execution remains usable. Local docids
    # always remain search-admitted only.
    url_admission: Literal["searched_only", "searched_or_public_web"] = "searched_or_public_web"
    batch_deadline_seconds: float = Field(default=60.0, gt=0.0)


class ExtractionCapabilitySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Byte limits use UTF-8 encoded request sizes so non-ASCII inputs cannot
    # exceed the admission budget while appearing short in Python characters.
    max_items: int = Field(default=256, ge=1)
    max_instruction_bytes: int = Field(default=16_384, ge=1)
    max_schema_bytes: int = Field(default=65_536, ge=1)
    max_item_bytes: int = Field(default=65_536, ge=1)
    max_total_item_bytes: int = Field(default=2_097_152, ge=1)
    max_schema_depth: int = Field(default=8, ge=1)
    max_repair_attempts: int = Field(default=1, ge=0, le=1)


class CapabilitySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    search: SearchCapabilitySettings = Field(default_factory=SearchCapabilitySettings)
    content: ContentCapabilitySettings = Field(default_factory=ContentCapabilitySettings)
    extraction: ExtractionCapabilitySettings = Field(default_factory=ExtractionCapabilitySettings)


class ProviderServicePolicySettings(BaseModel):
    """Optional deployment overrides for one reusable provider service."""

    model_config = ConfigDict(extra="forbid")

    concurrency: int | None = Field(default=None, ge=1)
    requests_per_second: float | None = Field(default=None, gt=0.0)
    burst: int | None = Field(default=None, ge=1)
    attempt_timeout_seconds: float | None = Field(default=None, gt=0.0)
    logical_deadline_seconds: float | None = Field(default=None, gt=0.0)

    @model_validator(mode="after")
    def validate_rate_limit(self) -> ProviderServicePolicySettings:
        if self.burst is not None and self.requests_per_second is None:
            raise ValueError("burst requires requests_per_second")
        return self


class ProviderServicesSettings(BaseModel):
    """Fixed execution-policy slots for reusable broker services."""

    model_config = ConfigDict(extra="forbid")

    search: ProviderServicePolicySettings = Field(default_factory=ProviderServicePolicySettings)
    document: ProviderServicePolicySettings = Field(default_factory=ProviderServicePolicySettings)
    rerank: ProviderServicePolicySettings = Field(default_factory=ProviderServicePolicySettings)
    llm: ProviderServicePolicySettings = Field(default_factory=ProviderServicePolicySettings)


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: _UniqueKeyLoader,
    node: yaml.MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise ConfigurationError("YAML mapping keys must be scalar values") from exc
        if duplicate:
            raise ConfigurationError(f"Duplicate YAML key: {key!r}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="forbid")

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: Any,
        env_settings: Any,
        dotenv_settings: Any,
        file_secret_settings: Any,
    ) -> tuple[Any, ...]:
        del cls, settings_cls, env_settings, dotenv_settings, file_secret_settings
        return (init_settings,)

    data_dir: Path = Path(".opensac")
    broker_socket: Path = Path(".opensac/broker.sock")
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    api_key: str = ""
    # None enables the dashboard only for an explicitly loopback-bound API.
    dashboard_enabled: bool | None = None
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

    # Backend selection and connection settings are explicit and deployment-wide.
    # Sessions inherit one validated source family so callers cannot switch
    # corpora or credentials through the public API.
    backends: BackendSettings = Field(default_factory=BackendSettings)
    capabilities: CapabilitySettings = Field(default_factory=CapabilitySettings)
    model_api_key: str = ""
    serper_api_key: str = ""
    jina_api_key: str = ""
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
    # Each reusable service binds one policy runtime. Missing values inherit the
    # deployment-wide reliability defaults above and role-specific concurrency.
    provider_services: ProviderServicesSettings = Field(default_factory=ProviderServicesSettings)

    @model_validator(mode="after")
    def validate_optional_service_policies(self) -> Settings:
        llm_policy = self.provider_services.llm.model_dump(exclude_none=True)
        if self.backends.llm.provider == "none" and llm_policy:
            raise ValueError("providers.services.llm requires an enabled LLM provider")
        return self

    # 0.3.1 in-flight sharing. Disabled by default so upgrading cannot alter a
    # frozen baseline's latency or failure timing.
    provider_inflight_coalescing: bool = False
    provider_max_inflight_keys: int = Field(default=256, ge=1)
    provider_max_waiters_per_key: int = Field(default=64, ge=1)
    # Backends may opt successful results into a short process-local cache.
    # Zero keeps it disabled so upgrading does not change provider freshness or
    # accounting by default.
    provider_result_cache_ttl_seconds: float = Field(default=0.0, ge=0.0)
    provider_result_cache_max_bytes: int = Field(default=128_000_000, ge=1)

    @property
    def backend_name(self) -> Literal["local", "web"]:
        return "local" if self.backends.search.provider == "local" else "web"

    @model_validator(mode="after")
    def validate_backend_pair(self) -> Settings:
        pair = (self.backends.search.provider, self.backends.document.provider)
        if pair not in {("local", "local"), ("serper", "jina")}:
            raise ValueError(
                "backends.search and backends.document must use local + local or serper + jina"
            )
        return self

    @staticmethod
    def _is_loopback_host(host: str) -> bool:
        normalized = host.strip().removeprefix("[").removesuffix("]")
        if normalized.lower() == "localhost":
            return True
        try:
            return ip_address(normalized).is_loopback
        except ValueError:
            return False

    @property
    def dashboard_is_enabled(self) -> bool:
        if self.dashboard_enabled is not None:
            return self.dashboard_enabled
        return self._is_loopback_host(self.api_host)

    @model_validator(mode="after")
    def validate_dashboard_exposure(self) -> Settings:
        if (
            self.dashboard_enabled is True
            and not self._is_loopback_host(self.api_host)
            and not self.api_key
        ):
            raise ValueError(
                "dashboard.enabled on a non-loopback API host requires OPENSAC_API_KEY"
            )
        return self

    sandbox_image: str = DEFAULT_SANDBOX_IMAGE
    # The API may itself run in a Linux container while talking to a macOS
    # Docker Desktop daemon. Keep the daemon host explicit so broker socket
    # mounts use Docker Desktop's socket-forwarding-compatible syntax.
    sandbox_docker_host_platform: Literal["linux", "darwin"] = (
        "darwin" if sys.platform == "darwin" else "linux"
    )
    # `cold` preserves one docker run per execution. `warm` keeps one hardened
    # container per active session while still starting a fresh Python process
    # for every program.
    sandbox_mode: Literal["cold", "warm"] = "cold"
    # Experimental session-scoped REPL. Disabled by default so deployments do
    # not pin one interpreter container per active session unless they opt in.
    experimental_persistent_interpreter: bool = False
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


def _legacy_env_names() -> set[str]:
    secret_fields = set(_SECRET_ENV_FIELDS.values())
    names = {
        f"OPENSAC_{name.upper()}" for name in Settings.model_fields if name not in secret_fields
    }
    names.update(
        {
            "OPENSAC_LOCAL_SEARCH_BASE_URL",
            "OPENSAC_MODEL_BASE_URL",
            "OPENSAC_MODEL_NAME",
            "OPENSAC_SEARCH_BACKEND",
            "OPENSAC_CONTENT_BATCH_DEADLINE_SECONDS",
            "OPENSAC_CONTENT_MAX_SOURCES_PER_REQUEST",
            "OPENSAC_CONTENT_URL_ADMISSION",
            "OPENSAC_EXTRACT_MAX_INSTRUCTION_BYTES",
            "OPENSAC_EXTRACT_MAX_ITEM_BYTES",
            "OPENSAC_EXTRACT_MAX_ITEMS",
            "OPENSAC_EXTRACT_MAX_REPAIR_ATTEMPTS",
            "OPENSAC_EXTRACT_MAX_SCHEMA_BYTES",
            "OPENSAC_EXTRACT_MAX_SCHEMA_DEPTH",
            "OPENSAC_EXTRACT_MAX_TOTAL_ITEM_BYTES",
            "OPENSAC_PASSAGE_RANKER",
            "OPENSAC_PASSAGE_RERANKER_MODEL",
            "OPENSAC_SEARCH_MAX_QUERIES_PER_REQUEST",
            "OPENSAC_SEARCH_MAX_QUERY_CHARS",
            "OPENSAC_SEARCH_MAX_TOP_K",
        }
    )
    return names


def _read_dotenv(path: Path) -> dict[str, str | None]:
    if not path.is_file():
        return {}
    values = dotenv_values(path)
    unknown = sorted(set(values) - set(_SECRET_ENV_FIELDS))
    if unknown:
        formatted = ", ".join(unknown)
        raise ConfigurationError(
            f"{path} may contain only OpenSAC API keys; move these settings to YAML: {formatted}"
        )
    return dict(values)


def _secret_values(dotenv_path: Path) -> dict[str, str]:
    legacy = sorted(name for name in _legacy_env_names() if name in os.environ)
    if legacy:
        formatted = ", ".join(legacy)
        raise ConfigurationError(
            "Non-secret OpenSAC environment variables are no longer supported; "
            f"move these settings to YAML: {formatted}"
        )

    dotenv = _read_dotenv(dotenv_path)
    values: dict[str, str] = {}
    for env_name, field_name in _SECRET_ENV_FIELDS.items():
        value = os.environ.get(env_name)
        if value is None:
            value = dotenv.get(env_name)
        if value is not None:
            values[field_name] = value
    return values


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigurationError(f"Cannot read OpenSAC configuration {path}: {exc}") from exc

    try:
        loaded = yaml.load(text, Loader=_UniqueKeyLoader)
    except ConfigurationError:
        raise
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"Invalid YAML in {path}: {exc}") from exc

    if loaded is None:
        return {}
    if not isinstance(loaded, Mapping):
        raise ConfigurationError(f"OpenSAC configuration {path} must contain a YAML mapping")

    flattened: dict[str, Any] = {}
    for section, section_values in loaded.items():
        if not isinstance(section, str) or (
            section not in _YAML_FIELDS and section not in _NESTED_YAML_FIELDS
        ):
            raise ConfigurationError(f"Unknown OpenSAC configuration section: {section!r}")
        if not isinstance(section_values, Mapping):
            raise ConfigurationError(f"Configuration section '{section}' must be a mapping")
        if section in _NESTED_YAML_FIELDS:
            nested_schema = _NESTED_YAML_FIELDS[section]
            item_label = "backend" if section == "backends" else "capability"
            nested_values: dict[str, Any] = {}
            for item_kind, item_config in section_values.items():
                if not isinstance(item_kind, str) or item_kind not in nested_schema:
                    raise ConfigurationError(
                        f"Unknown OpenSAC configuration {item_label}: {section}.{item_kind}"
                    )
                if not isinstance(item_config, Mapping):
                    raise ConfigurationError(
                        f"Configuration {item_label} '{section}.{item_kind}' must be a mapping"
                    )
                parsed_item: dict[str, Any] = {}
                for name, value in item_config.items():
                    secret_env = (
                        _SECRET_BACKEND_YAML_FIELDS.get((item_kind, name))
                        if section == "backends"
                        else None
                    )
                    if secret_env is not None:
                        raise ConfigurationError(
                            f"Secret '{section}.{item_kind}.{name}' is not allowed in YAML; "
                            f"use {secret_env}"
                        )
                    if name not in nested_schema[item_kind]:
                        raise ConfigurationError(
                            f"Unknown OpenSAC configuration field: {section}.{item_kind}.{name}"
                        )
                    parsed_item[name] = value
                nested_values[item_kind] = parsed_item
            flattened[section] = nested_values
            continue
        for name, value in section_values.items():
            secret_env = _SECRET_YAML_FIELDS.get((section, name))
            if secret_env is not None:
                raise ConfigurationError(
                    f"Secret '{section}.{name}' is not allowed in YAML; use {secret_env}"
                )
            field_name = _YAML_FIELDS[section].get(name)
            if field_name is None:
                raise ConfigurationError(f"Unknown OpenSAC configuration field: {section}.{name}")
            flattened[field_name] = value

    for field_name in ("data_dir", "broker_socket"):
        value = flattened.get(field_name)
        if value is None:
            continue
        configured_path = Path(value).expanduser()
        if not configured_path.is_absolute():
            configured_path = path.parent / configured_path
        flattened[field_name] = configured_path.resolve()
    return flattened


def load_settings(config_path: Path | None = None) -> Settings:
    """Load one deployment configuration plus API keys from env or `.env`."""
    path = config_path.expanduser().resolve() if config_path is not None else None
    values = _load_yaml(path) if path is not None else {}
    values.update(_secret_values(Path(".env")))
    try:
        return Settings(**values)
    except ValidationError as exc:
        location = f" in {path}" if path is not None else ""
        raise ConfigurationError(f"Invalid OpenSAC configuration{location}: {exc}") from exc
