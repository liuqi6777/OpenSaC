from __future__ import annotations

import inspect
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from opensac.broker.registry import BaseCapabilities, capability_types_from_module

if TYPE_CHECKING:
    from opensac.backends.document import DocumentBackend
    from opensac.backends.llm import LLMBackend
    from opensac.backends.rerank import TextReranker
    from opensac.backends.search import SearchBackend
    from opensac.broker.config import BrokerConfig
    from opensac.broker.providers import BackendBinding, ProviderExecutor
    from opensac.broker.session import BrokerSession


@dataclass(frozen=True, slots=True)
class CapabilityBuildContext:
    providers: ProviderExecutor
    search_bindings: dict[str, BackendBinding[SearchBackend]]
    document_bindings: dict[str, BackendBinding[DocumentBackend]]
    rerank_binding: BackendBinding[TextReranker]
    llm_binding: BackendBinding[LLMBackend] | None
    config: BrokerConfig
    default_provider_concurrency: int
    session_manifest: Callable[[BrokerSession], dict[str, Any]]


class CapabilityCatalog:
    """Assemble discovered capability classes behind the fixed core RPC contract."""

    def __init__(self) -> None:
        self.module_types: dict[str, type[BaseCapabilities]] = {}
        self._sources: dict[str, str] = {}

    def register(
        self,
        module_types: Iterable[type[BaseCapabilities]],
        *,
        source: str,
    ) -> None:
        for module_type in module_types:
            if (
                not inspect.isclass(module_type)
                or not issubclass(module_type, BaseCapabilities)
                or inspect.isabstract(module_type)
            ):
                raise TypeError(
                    f"Capability plugin {source!r} must contain concrete "
                    "BaseCapabilities subclasses"
                )
            if not isinstance(module_type.name, str):
                raise TypeError(f"Capability plugin {source!r} module name must be a string")
            name = module_type.name.strip()
            if not name:
                raise ValueError(f"Capability plugin {source!r} registered an empty module name")
            if name in self._sources:
                raise ValueError(
                    f"Duplicate capability module {name!r} from {source!r}; "
                    f"already registered by {self._sources[name]!r}"
                )
            self.module_types[name] = module_type
            self._sources[name] = source

    @classmethod
    def builtin(cls) -> CapabilityCatalog:
        from opensac.broker.capabilities import content, llm, search, session

        catalog = cls()
        for module in (search, content, session, llm):
            catalog.register(capability_types_from_module(module), source="opensac")
        return catalog

    def assemble(
        self,
        context: CapabilityBuildContext,
        *,
        enabled: Iterable[str] | None = None,
    ) -> tuple[BaseCapabilities, ...]:
        names = tuple(enabled) if enabled is not None else tuple(self.module_types)
        modules: list[BaseCapabilities] = []
        seen: set[str] = set()
        for name in names:
            if name in seen:
                raise ValueError(f"Capability module {name!r} was enabled more than once")
            seen.add(name)
            try:
                module_type = self.module_types[name]
            except KeyError as exc:
                available = ", ".join(self.module_types) or "none"
                raise ValueError(
                    f"Unknown capability module {name!r}; available modules: {available}"
                ) from exc
            module = module_type.from_context(context)
            if not isinstance(module, BaseCapabilities):
                raise TypeError(
                    f"Capability module {name!r} produced {type(module).__name__}, "
                    "expected BaseCapabilities"
                )
            if module.name != name:
                raise ValueError(f"Capability class {name!r} produced module {module.name!r}")
            if not isinstance(module.available, bool):
                raise TypeError(f"Capability module {name!r} available must be a boolean")
            modules.append(module)
        return tuple(modules)
