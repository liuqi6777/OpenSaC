from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from opensac.backends.catalog import BackendBuildContext, BackendCatalog
from opensac.broker.capabilities.catalog import CapabilityCatalog
from opensac.broker.config import BrokerConfig
from opensac.broker.plugins import (
    BrokerPlugin,
    BrokerPluginRegistration,
    builtin_broker_plugin,
    discover_broker_plugins,
    validate_broker_plugin,
)
from opensac.broker.providers import ProviderExecutionConfig
from opensac.broker.runtime import BrokerRuntime
from opensac.broker.service import BrokerService, CapabilityObserver, RetrievalRoute
from opensac.config import DEFAULT_LOCAL_BACKEND_BASE_URL, ProviderServicePolicySettings, Settings
from opensac.models import CAPABILITY_CONTRACT, Mechanisms
from opensac.provider import ProviderPolicy, ProviderRuntime


@dataclass(frozen=True, slots=True)
class BrokerAssembly:
    """A fully assembled broker plus the deployment metadata it owns."""

    broker: BrokerService
    runtime: BrokerRuntime
    backend_name: str
    provider_services: tuple[str, ...]
    settings: Settings = field(repr=False)

    @property
    def socket_path(self) -> Path:
        return self.runtime.socket_path

    def environment_manifest(self, mechanisms: Mechanisms | None = None) -> dict[str, Any]:
        sdk_capabilities = self.broker.capability_manifest(
            backend_name=self.backend_name,
            mechanisms=mechanisms or Mechanisms(),
        )
        capability_limits = self._capability_limits(sdk_capabilities)
        return {
            "capability_contract": CAPABILITY_CONTRACT,
            "sdk_capabilities": sdk_capabilities,
            "capability_limits": capability_limits,
            "service_policies": {
                name: {
                    **asdict(self.broker.service_runtimes[name].policy),
                    "max_attempts": self.broker.service_runtimes[
                        name
                    ].policy.effective_max_attempts,
                }
                for name in self.provider_services
            },
            "backend_revision": self.settings.backend_revision,
            "backend_metadata_hash": self.settings.backend_metadata_hash,
            "search_backend": self.backend_name,
            "passage_ranker": self.settings.backends.rerank.provider,
            "local_search_base_url": (
                self.settings.backends.search.base_url or DEFAULT_LOCAL_BACKEND_BASE_URL
            ),
        }

    def _capability_limits(self, sdk_capabilities: Mapping[str, Any]) -> dict[str, Any]:
        settings = self.settings
        cacheable_services = [
            name
            for name, backends in (
                ("search", self.broker.search_backends.values()),
                ("document", self.broker.document_backends.values()),
            )
            if any(getattr(backend, "result_cacheable", False) for backend in backends)
        ]
        limits: dict[str, Any] = {
            "inflight": {
                "enabled": settings.provider_inflight_coalescing,
                "max_keys": settings.provider_max_inflight_keys,
                "max_waiters_per_key": settings.provider_max_waiters_per_key,
            },
            "provider_result_cache": {
                "enabled": settings.provider_result_cache_ttl_seconds > 0,
                "ttl_seconds": settings.provider_result_cache_ttl_seconds,
                "max_bytes": settings.provider_result_cache_max_bytes,
                "services": cacheable_services,
            },
        }
        if search := sdk_capabilities.get("search"):
            search_limits = search["limits"]
            limits["search"] = {
                "max_queries_per_request": search_limits["max_queries_per_request"],
                "max_query_chars": search_limits["max_query_chars"],
                "max_top_k": search_limits["max_top_k"],
            }
        if llm := sdk_capabilities.get("llm"):
            llm_limits = llm["limits"]
            limits["extract"] = {
                "max_instruction_bytes": llm_limits["extract_max_instruction_bytes"],
                "max_schema_bytes": llm_limits["extract_max_schema_bytes"],
                "max_item_bytes": llm_limits["extract_max_item_bytes"],
                "max_schema_depth": llm_limits["extract_max_schema_depth"],
                "max_repair_attempts": llm_limits["extract_max_repair_attempts"],
            }
        if content := sdk_capabilities.get("content"):
            content_limits = content["limits"]
            limits["content"] = {
                "max_sources_per_request": content_limits["max_sources_per_request"],
                "url_admission": content["url_admission"],
                "batch_deadline_seconds": self.broker.config.content.batch_deadline_seconds,
                "passage_limit": content_limits["passage_limit"],
                "passage_limit_per_source": content_limits["passage_limit_per_source"],
                "passage_chunk_chars": self.broker.config.content.passage_chunk_chars,
                "passage_chunk_overlap_chars": (
                    self.broker.config.content.passage_chunk_overlap_chars
                ),
                "passage_prefilter_limit": self.broker.config.content.passage_prefilter_limit,
            }
        return limits


class BrokerBuilder:
    """Compose provider policy, backend plugins, and capability modules."""

    def __init__(
        self,
        *,
        plugins: Iterable[BrokerPlugin] = (),
        discover_installed_plugins: bool = True,
        enabled_capabilities: Iterable[str] | None = None,
    ) -> None:
        self.plugins = tuple(plugins)
        self.discover_installed_plugins = discover_installed_plugins
        self.enabled_capabilities = (
            tuple(enabled_capabilities) if enabled_capabilities is not None else None
        )

    def build(
        self,
        settings: Settings,
        *,
        capability_observer: CapabilityObserver | None = None,
    ) -> BrokerAssembly:
        service_configs = self._service_configs(settings)
        service_policies = self._service_policies(settings, service_configs)
        service_runtimes = {
            name: ProviderRuntime(policy) for name, policy in service_policies.items()
        }
        backend_catalog, capability_catalog = self._catalogs()
        backend = backend_catalog.assemble(
            BackendBuildContext(
                settings=settings,
                service_policies=service_policies,
            )
        )
        broker = BrokerService(
            {
                backend.route_name: RetrievalRoute(
                    search=backend.search,
                    document=backend.document,
                    revision=settings.backend_revision,
                )
            },
            config=BrokerConfig.from_settings(settings),
            llm_backend=backend.llm,
            reranker=backend.reranker,
            max_concurrency=settings.max_concurrency,
            provider_execution_config=ProviderExecutionConfig(
                inflight_coalescing=settings.provider_inflight_coalescing,
                max_inflight_keys=settings.provider_max_inflight_keys,
                max_waiters_per_flight=settings.provider_max_waiters_per_key,
                result_cache_ttl_seconds=settings.provider_result_cache_ttl_seconds,
                result_cache_max_bytes=settings.provider_result_cache_max_bytes,
            ),
            search_runtime=service_runtimes["search"],
            document_runtime=service_runtimes["document"],
            rerank_runtime=service_runtimes["rerank"],
            llm_runtime=service_runtimes.get("llm"),
            capability_observer=capability_observer,
            capability_catalog=capability_catalog,
            enabled_capabilities=self.enabled_capabilities,
        )
        return BrokerAssembly(
            broker=broker,
            runtime=BrokerRuntime(broker, settings.broker_socket),
            backend_name=backend.route_name,
            provider_services=tuple(service_configs),
            settings=settings,
        )

    def _catalogs(self) -> tuple[BackendCatalog, CapabilityCatalog]:
        registrations = [BrokerPluginRegistration(plugin=builtin_broker_plugin(), source="opensac")]
        if self.discover_installed_plugins:
            registrations.extend(discover_broker_plugins())
        registrations.extend(
            BrokerPluginRegistration(
                plugin=plugin,
                source=f"programmatic plugin {index}",
            )
            for index, plugin in enumerate(self.plugins, start=1)
        )

        backend_catalog = BackendCatalog()
        capability_catalog = CapabilityCatalog()
        for registration in registrations:
            validate_broker_plugin(registration.plugin, source=registration.source)
            backend_catalog.register(
                registration.plugin.backends,
                source=registration.source,
            )
            capability_catalog.register(
                registration.plugin.capabilities,
                source=registration.source,
            )
        return backend_catalog, capability_catalog

    @staticmethod
    def _service_configs(
        settings: Settings,
    ) -> dict[str, ProviderServicePolicySettings]:
        configs = {
            "search": settings.provider_services.search,
            "document": settings.provider_services.document,
            "rerank": settings.provider_services.rerank,
        }
        if settings.backends.llm.provider != "none":
            configs["llm"] = settings.provider_services.llm
        return configs

    @staticmethod
    def _service_policies(
        settings: Settings,
        service_configs: Mapping[str, ProviderServicePolicySettings],
    ) -> dict[str, ProviderPolicy]:
        default_concurrency = {
            "search": settings.max_concurrency,
            "document": settings.backend_fetch_concurrency,
            "rerank": 2,
            "llm": settings.max_concurrency,
        }
        return {
            name: ProviderPolicy(
                retry_profile=settings.provider_retry_profile,
                max_attempts=settings.provider_max_attempts,
                attempt_timeout_seconds=(
                    config.attempt_timeout_seconds or settings.provider_attempt_timeout_seconds
                ),
                logical_deadline_seconds=(
                    config.logical_deadline_seconds or settings.provider_logical_deadline_seconds
                ),
                base_backoff_seconds=settings.provider_base_backoff_seconds,
                max_backoff_seconds=settings.provider_max_backoff_seconds,
                max_total_backoff_seconds=settings.provider_max_total_backoff_seconds,
                max_retry_after_seconds=settings.provider_max_retry_after_seconds,
                concurrency=config.concurrency or default_concurrency[name],
                requests_per_second=config.requests_per_second,
                burst=config.burst,
            )
            for name, config in service_configs.items()
        }
