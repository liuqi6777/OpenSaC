from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from types import ModuleType
from typing import Any, Literal

from opensac.backends.document import DocumentBackend
from opensac.backends.llm import LLMBackend
from opensac.backends.rerank import TextReranker
from opensac.backends.search import SearchBackend
from opensac.config import Settings
from opensac.provider import ProviderPolicy

type BackendRole = Literal["search", "document", "rerank", "llm"]
type BackendFactory = Callable[["BackendBuildContext"], Any]

_BACKEND_PROVIDER_ATTRIBUTE = "__opensac_backend_provider__"


@dataclass(frozen=True, slots=True)
class BackendBuildContext:
    settings: Settings
    service_policies: Mapping[str, ProviderPolicy]

    def timeout(self, service: str) -> float:
        try:
            return self.service_policies[service].attempt_timeout_seconds
        except KeyError as exc:
            raise ValueError(f"Backend factory requires unconfigured service {service!r}") from exc


@dataclass(frozen=True, slots=True)
class BackendProvider:
    """Declarative registration for one provider factory and backend role."""

    role: BackendRole
    name: str
    factory: BackendFactory

    def __post_init__(self) -> None:
        if self.role not in {"search", "document", "rerank", "llm"}:
            raise ValueError(f"Unsupported backend role: {self.role!r}")
        if not isinstance(self.name, str):
            raise TypeError("Backend provider name must be a string")
        normalized_name = self.name.strip()
        if not normalized_name:
            raise ValueError("Backend provider name must not be empty")
        if not callable(self.factory):
            raise TypeError("Backend provider factory must be callable")
        object.__setattr__(self, "name", normalized_name)


def backend_provider[FactoryT: BackendFactory](
    *,
    role: BackendRole,
    name: str,
) -> Callable[[FactoryT], FactoryT]:
    """Mark a factory for bounded module discovery by a broker plugin."""

    def decorate(factory: FactoryT) -> FactoryT:
        if hasattr(factory, _BACKEND_PROVIDER_ATTRIBUTE):
            raise ValueError(f"Backend factory {factory.__name__!r} is already registered")
        setattr(
            factory,
            _BACKEND_PROVIDER_ATTRIBUTE,
            BackendProvider(role=role, name=name, factory=factory),
        )
        return factory

    return decorate


def backend_providers_from_module(module: ModuleType) -> tuple[BackendProvider, ...]:
    """Collect only factories declared and explicitly marked in one loaded module."""

    providers: list[BackendProvider] = []
    seen: set[int] = set()
    for value in vars(module).values():
        provider = getattr(value, _BACKEND_PROVIDER_ATTRIBUTE, None)
        if not isinstance(provider, BackendProvider):
            continue
        if getattr(value, "__module__", None) != module.__name__:
            continue
        if id(provider) in seen:
            continue
        seen.add(id(provider))
        providers.append(provider)
    return tuple(providers)


@dataclass(frozen=True, slots=True)
class BackendAssembly:
    route_name: str
    search: SearchBackend
    document: DocumentBackend
    reranker: TextReranker
    llm: LLMBackend | None


class BackendCatalog:
    """Resolve deployment-selected factories from validated provider descriptors."""

    def __init__(self) -> None:
        self.search: dict[str, BackendFactory] = {}
        self.document: dict[str, BackendFactory] = {}
        self.rerank: dict[str, BackendFactory] = {}
        self.llm: dict[str, BackendFactory] = {}
        self._sources: dict[tuple[str, str], str] = {}

    def register(self, providers: Iterable[BackendProvider], *, source: str) -> None:
        for provider in providers:
            if not isinstance(provider, BackendProvider):
                raise TypeError(f"Backend plugin {source!r} contains an invalid provider")
            target: dict[str, BackendFactory] = getattr(self, provider.role)
            key = (provider.role, provider.name)
            if key in self._sources:
                raise ValueError(
                    f"Duplicate {provider.role} backend provider {provider.name!r} from "
                    f"{source!r}; already registered by {self._sources[key]!r}"
                )
            target[provider.name] = provider.factory
            self._sources[key] = source

    @classmethod
    def builtin(cls) -> BackendCatalog:
        from opensac.backends import builtin as builtin_module

        catalog = cls()
        catalog.register(backend_providers_from_module(builtin_module), source="opensac")
        return catalog

    def assemble(self, context: BackendBuildContext) -> BackendAssembly:
        settings = context.settings.backends
        search = self._create("search", settings.search.provider, context)
        document = self._create("document", settings.document.provider, context)
        reranker = self._create("rerank", settings.rerank.provider, context)
        llm = (
            None
            if settings.llm.provider == "none"
            else self._create("llm", settings.llm.provider, context)
        )
        self._validate_backend("search", search)
        self._validate_backend("document", document)
        self._validate_backend("rerank", reranker)
        if llm is not None:
            self._validate_backend("llm", llm)

        search_route = str(search.name).strip()
        document_route = str(document.name).strip()
        if search_route != document_route:
            raise ValueError(
                "Search and document backends must declare the same route name; "
                f"got {search_route!r} and {document_route!r}"
            )
        return BackendAssembly(
            route_name=search_route,
            search=search,
            document=document,
            reranker=reranker,
            llm=llm,
        )

    def _create(self, role: str, provider: str, context: BackendBuildContext) -> Any:
        factories: dict[str, Callable[[BackendBuildContext], Any]] = getattr(self, role)
        try:
            factory = factories[provider]
        except KeyError as exc:
            available = ", ".join(sorted(factories)) or "none"
            raise ValueError(
                f"Unknown {role} backend provider {provider!r}; available providers: {available}"
            ) from exc
        backend = factory(context)
        if backend is None:
            raise TypeError(f"{role.title()} backend provider {provider!r} returned None")
        return backend

    @staticmethod
    def _validate_backend(role: str, backend: Any) -> None:
        name = str(getattr(backend, "name", "")).strip()
        identity = str(getattr(backend, "provider_identity", "")).strip()
        if not name:
            raise TypeError(f"{role.title()} backend must declare a non-empty name")
        if not identity:
            raise TypeError(f"{role.title()} backend must declare a provider_identity")

        required_methods = {
            "search": ("search",),
            "document": ("fetch_candidates", "fetch"),
            "rerank": ("preflight", "rerank"),
            "llm": ("complete",),
        }[role]
        for method in required_methods:
            if not callable(getattr(backend, method, None)):
                raise TypeError(f"{role.title()} backend must implement {method}()")

        if role == "search":
            if not isinstance(getattr(backend, "result_cacheable", None), bool):
                raise TypeError("Search backend result_cacheable must be a boolean")
            if not isinstance(getattr(backend, "supports_domains", None), bool):
                raise TypeError("Search backend supports_domains must be a boolean")
            max_depth = getattr(backend, "max_depth", None)
            if max_depth is not None and (not isinstance(max_depth, int) or max_depth < 1):
                raise TypeError("Search backend max_depth must be a positive integer or None")
        elif role == "document":
            if not isinstance(getattr(backend, "result_cacheable", None), bool):
                raise TypeError("Document backend result_cacheable must be a boolean")
            if getattr(backend, "source_kind", None) not in {"opaque", "public_url"}:
                raise TypeError("Document backend source_kind must be 'opaque' or 'public_url'")
