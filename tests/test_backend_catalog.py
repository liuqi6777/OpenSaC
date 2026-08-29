from __future__ import annotations

from dataclasses import dataclass
from types import ModuleType
from typing import Any

import pytest

from opensac.backends import BackendBuildContext, BackendProvider, backend_provider
from opensac.backends.catalog import BackendCatalog
from opensac.broker import (
    BaseCapabilities,
    BrokerBuilder,
    BrokerPlugin,
    capability_method,
)
from opensac.broker import plugins as plugins_module
from opensac.broker.capabilities.catalog import CapabilityCatalog
from opensac.broker.capabilities.search import SearchCapabilities
from opensac.broker.plugins import (
    BROKER_PLUGIN_API_VERSION,
    BROKER_PLUGIN_ENTRY_POINT_GROUP,
    discover_broker_plugins,
)
from opensac.broker.registry import EmptyRequest
from opensac.config import Settings
from opensac.provider import ProviderPolicy


class CustomSearchBackend:
    result_cacheable = False
    provider_identity = "custom-search:test"
    supports_domains = False
    max_depth = None

    def __init__(self, route: str) -> None:
        self.name = route

    async def search(self, query, *, limit, offset=0, domains=None):
        del query, limit, offset, domains
        return []


class CustomDocumentBackend:
    source_kind = "opaque"
    result_cacheable = False
    provider_identity = "custom-document:test"

    def __init__(self, route: str) -> None:
        self.name = route

    @staticmethod
    def fetch_candidates(handle):
        return [handle]

    async def fetch(self, handle, *, query=None):
        del handle, query
        raise AssertionError("not called")


class CustomReranker:
    name = "custom"
    provider_identity = "custom-reranker:test"

    @staticmethod
    def preflight() -> None:
        return None

    async def rerank(self, query, documents):
        del query, documents
        return []


def build_context(settings: Settings) -> BackendBuildContext:
    return BackendBuildContext(
        settings=settings,
        service_policies={
            "search": ProviderPolicy(concurrency=2),
            "document": ProviderPolicy(concurrency=3),
            "rerank": ProviderPolicy(concurrency=1),
        },
    )


def custom_plugin(*, document_route: str = "custom") -> BrokerPlugin:
    return BrokerPlugin(
        backends=(
            BackendProvider(
                role="search",
                name="custom_search",
                factory=lambda context: CustomSearchBackend("custom"),
            ),
            BackendProvider(
                role="document",
                name="custom_document",
                factory=lambda context: CustomDocumentBackend(document_route),
            ),
            BackendProvider(
                role="rerank",
                name="custom_rerank",
                factory=lambda context: CustomReranker(),
            ),
        )
    )


def custom_settings() -> Settings:
    return Settings(
        backends={
            "search": {
                "provider": "custom_search",
                "options": {"endpoint": "https://search.example.test"},
            },
            "document": {"provider": "custom_document"},
            "rerank": {"provider": "custom_rerank"},
        }
    )


def test_builtin_backends_use_declarative_catalog_assembly() -> None:
    catalog = BackendCatalog.builtin()

    assembly = catalog.assemble(build_context(Settings()))

    assert assembly.route_name == "local"
    assert assembly.search.name == assembly.document.name == "local"
    assert assembly.reranker.name == "lexical:bm25"
    assert assembly.llm is None

    configured_options = Settings(backends={"search": {"options": {"endpoint": "typo"}}})
    with pytest.raises(ValueError, match="does not accept options"):
        catalog.assemble(build_context(configured_options))


def test_injected_backend_provider_receives_settings() -> None:
    observed: dict[str, object] = {}

    def search(context: BackendBuildContext):
        observed.update(context.settings.backends.search.options)
        return CustomSearchBackend("custom")

    plugin = custom_plugin()
    plugin = BrokerPlugin(
        backends=(
            BackendProvider("search", "custom_search", search),
            *plugin.backends[1:],
        )
    )
    catalog = BackendCatalog()
    catalog.register(plugin.backends, source="test")

    assembly = catalog.assemble(build_context(custom_settings()))

    assert assembly.route_name == "custom"
    assert observed == {"endpoint": "https://search.example.test"}


def test_broker_builder_assembles_one_plugin_and_capability_subset() -> None:
    assembly = BrokerBuilder(
        plugins=(custom_plugin(),),
        discover_installed_plugins=False,
        enabled_capabilities=("search", "content", "session"),
    ).build(custom_settings())

    assert assembly.backend_name == "custom"
    assert assembly.runtime.service is assembly.broker
    assert assembly.socket_path == assembly.runtime.socket_path
    assert assembly.provider_services == ("search", "document", "rerank")
    assert assembly.broker.registry.module_names == ("search", "content", "session")
    manifest = assembly.environment_manifest()
    assert manifest["search_backend"] == "custom"
    assert set(manifest["sdk_capabilities"]) == {
        "contracts",
        "search",
        "content",
        "mechanisms",
    }
    assert "extract" not in manifest["capability_limits"]


def test_backend_catalog_rejects_unknown_duplicate_and_mismatched_routes() -> None:
    builtins = BackendCatalog.builtin()
    unknown = custom_settings().model_copy(
        update={
            "backends": custom_settings().backends.model_copy(
                update={
                    "search": custom_settings().backends.search.model_copy(
                        update={"provider": "missing"}
                    )
                }
            )
        }
    )
    with pytest.raises(ValueError, match="Unknown search backend provider 'missing'"):
        builtins.assemble(build_context(unknown))

    with pytest.raises(ValueError, match="Duplicate search backend provider 'local'"):
        builtins.register(
            (BackendProvider("search", "local", lambda context: CustomSearchBackend("local")),),
            source="duplicate",
        )

    mismatched = BackendCatalog()
    mismatched.register(custom_plugin(document_route="other").backends, source="test")
    with pytest.raises(ValueError, match="same route name"):
        mismatched.assemble(build_context(custom_settings()))


def test_backend_catalog_validates_provider_protocol_at_startup() -> None:
    class IncompleteSearch:
        name = "custom"
        provider_identity = "incomplete:test"
        result_cacheable = False
        supports_domains = False
        max_depth = None

    plugin = custom_plugin()
    catalog = BackendCatalog()
    catalog.register(
        (
            BackendProvider("search", "custom_search", lambda context: IncompleteSearch()),
            *plugin.backends[1:],
        ),
        source="test",
    )

    with pytest.raises(TypeError, match=r"Search backend must implement search\(\)"):
        catalog.assemble(build_context(custom_settings()))


def test_broker_builder_rejects_incompatible_plugin_api() -> None:
    with pytest.raises(ValueError, match=f"expected {BROKER_PLUGIN_API_VERSION}"):
        BrokerBuilder(
            plugins=(BrokerPlugin(api_version=BROKER_PLUGIN_API_VERSION + 1),),
            discover_installed_plugins=False,
        ).build(Settings())


def test_capability_catalog_rejects_duplicate_discovered_modules() -> None:
    catalog = CapabilityCatalog.builtin()

    with pytest.raises(ValueError, match="Duplicate capability module 'search'"):
        catalog.register((SearchCapabilities,), source="test")


def decorated_plugin_module() -> ModuleType:
    module = ModuleType("example_opensac_plugin")

    @backend_provider(role="search", name="module_search")
    def module_search(context: BackendBuildContext):
        return CustomSearchBackend("module")

    def undecorated(context: BackendBuildContext):
        return CustomSearchBackend("ignored")

    class ModuleCapabilities(BaseCapabilities):
        name = "module"

        @classmethod
        def from_context(cls, context: Any):
            del context
            return cls()

        @staticmethod
        def _trace_queries(request: EmptyRequest) -> list[str]:
            del request
            return ["ping"]

        @staticmethod
        def _trace_input_count(request: EmptyRequest) -> int:
            del request
            return 2

        @staticmethod
        def _trace_result_count(result: Any) -> int:
            return len(str(result))

        @capability_method(
            "module.ping",
            EmptyRequest,
            trace_queries="_trace_queries",
            trace_input_count="_trace_input_count",
            trace_result_count="_trace_result_count",
        )
        async def ping(self, state, request):
            del self, state, request
            return "pong"

    module_search.__module__ = module.__name__
    undecorated.__module__ = module.__name__
    ModuleCapabilities.__module__ = module.__name__
    module.module_search = module_search
    module.undecorated = undecorated
    module.ModuleCapabilities = ModuleCapabilities
    module.ImportedCapabilities = SearchCapabilities
    return module


def test_broker_plugin_discovers_only_local_decorated_components() -> None:
    plugin = BrokerPlugin.from_modules(decorated_plugin_module())

    assert [(provider.role, provider.name) for provider in plugin.backends] == [
        ("search", "module_search")
    ]
    assert [module_type.name for module_type in plugin.capabilities] == ["module"]
    module = plugin.capabilities[0].from_context(None)
    [spec] = module.specs()
    request = spec.parse({})
    assert spec.method == "module.ping"
    assert spec.trace.queries(request) == ["ping"]
    assert spec.trace.input_count(request) == 2
    assert spec.trace.result_count("pong") == 4


@dataclass
class FakeEntryPoint:
    name: str
    value: str
    loaded: object

    def load(self):
        return self.loaded


def test_broker_plugins_discover_installed_decorated_modules(monkeypatch) -> None:
    entry_point = FakeEntryPoint(
        "custom",
        "example_opensac_plugin",
        decorated_plugin_module(),
    )

    def entry_points(*, group: str):
        assert group == BROKER_PLUGIN_ENTRY_POINT_GROUP
        return [entry_point]

    monkeypatch.setattr(plugins_module.importlib_metadata, "entry_points", entry_points)

    registrations = discover_broker_plugins()

    assert len(registrations) == 1
    assert registrations[0].source == "entry point custom"
    assert registrations[0].plugin.backends[0].name == "module_search"
    assert registrations[0].plugin.capabilities[0].name == "module"


def test_broker_plugins_reject_invalid_entry_point_payload(monkeypatch) -> None:
    entry_point = FakeEntryPoint("broken", "example:broken", object())
    monkeypatch.setattr(
        plugins_module.importlib_metadata,
        "entry_points",
        lambda *, group: [entry_point],
    )

    with pytest.raises(TypeError, match="must load a BrokerPlugin"):
        discover_broker_plugins()
