from __future__ import annotations

from dataclasses import dataclass
from importlib import metadata as importlib_metadata
from types import ModuleType

from opensac.backends.catalog import BackendProvider, backend_providers_from_module
from opensac.broker.registry import BaseCapabilities, capability_types_from_module

BROKER_PLUGIN_API_VERSION = 1
BROKER_PLUGIN_ENTRY_POINT_GROUP = "opensac.broker_plugins"


@dataclass(frozen=True, slots=True)
class BrokerPlugin:
    """Backend providers and capability modules contributed by one trusted plugin."""

    api_version: int = BROKER_PLUGIN_API_VERSION
    backends: tuple[BackendProvider, ...] = ()
    capabilities: tuple[type[BaseCapabilities], ...] = ()

    @classmethod
    def from_modules(
        cls,
        *modules: ModuleType,
        api_version: int = BROKER_PLUGIN_API_VERSION,
    ) -> BrokerPlugin:
        backends: list[BackendProvider] = []
        capabilities: list[type[BaseCapabilities]] = []
        for module in modules:
            backends.extend(backend_providers_from_module(module))
            capabilities.extend(capability_types_from_module(module))
        return cls(
            api_version=api_version,
            backends=tuple(backends),
            capabilities=tuple(capabilities),
        )


@dataclass(frozen=True, slots=True)
class BrokerPluginRegistration:
    plugin: BrokerPlugin
    source: str


def builtin_broker_plugin() -> BrokerPlugin:
    from opensac.backends import builtin as backend_module
    from opensac.broker.capabilities import content, llm, search, session

    return BrokerPlugin.from_modules(
        backend_module,
        search,
        content,
        session,
        llm,
    )


def discover_broker_plugins() -> tuple[BrokerPluginRegistration, ...]:
    registrations: list[BrokerPluginRegistration] = []
    entry_points = importlib_metadata.entry_points(group=BROKER_PLUGIN_ENTRY_POINT_GROUP)
    for entry_point in sorted(entry_points, key=lambda item: (item.name, item.value)):
        loaded = entry_point.load()
        if isinstance(loaded, ModuleType):
            plugin = BrokerPlugin.from_modules(loaded)
        else:
            plugin = (
                loaded() if callable(loaded) and not isinstance(loaded, BrokerPlugin) else loaded
            )
        if not isinstance(plugin, BrokerPlugin):
            raise TypeError(
                f"Broker entry point {entry_point.name!r} must load a BrokerPlugin, "
                "a decorated module, or a zero-argument plugin factory"
            )
        registrations.append(
            BrokerPluginRegistration(
                plugin=plugin,
                source=f"entry point {entry_point.name}",
            )
        )
    return tuple(registrations)


def validate_broker_plugin(plugin: BrokerPlugin, *, source: str) -> None:
    if plugin.api_version != BROKER_PLUGIN_API_VERSION:
        raise ValueError(
            f"Broker plugin {source!r} uses API version {plugin.api_version}; "
            f"expected {BROKER_PLUGIN_API_VERSION}"
        )
