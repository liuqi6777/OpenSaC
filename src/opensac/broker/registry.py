from __future__ import annotations

import inspect
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from types import ModuleType
from typing import Any, ClassVar, Self, cast

from pydantic import BaseModel, ConfigDict

from opensac.broker.session import BrokerSession


class CapabilityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class EmptyRequest(CapabilityRequest):
    pass


type CapabilityHandler = Callable[[BrokerSession, CapabilityRequest], Awaitable[Any]]
type TraceQueries = Callable[[CapabilityRequest], list[str]]
type TraceCount = Callable[[CapabilityRequest], int]
type ResultCount = Callable[[Any], int]
type TraceHook = str | Callable[..., Any]

_CAPABILITY_METHOD_ATTRIBUTE = "__opensac_capability_method__"


def _no_queries(_request: CapabilityRequest) -> list[str]:
    return []


def _one_input(_request: CapabilityRequest) -> int:
    return 1


def _default_result_count(result: Any) -> int:
    if isinstance(result, list):
        return len(result)
    return 1 if result is not None else 0


@dataclass(frozen=True, slots=True)
class TraceSpec:
    queries: TraceQueries = _no_queries
    input_count: TraceCount = _one_input
    result_count: ResultCount = _default_result_count


@dataclass(frozen=True, slots=True)
class CapabilitySpec:
    method: str
    request_model: type[CapabilityRequest]
    handler: CapabilityHandler
    trace: TraceSpec

    def parse(self, params: dict[str, Any]) -> CapabilityRequest:
        return self.request_model.model_validate(params)


@dataclass(frozen=True, slots=True)
class CapabilityMethodDefinition:
    method: str
    request_model: type[CapabilityRequest]
    handler_name: str
    trace_queries: TraceHook | None = None
    trace_input_count: TraceHook | None = None
    trace_result_count: TraceHook | None = None


def capability_method[HandlerT: Callable[..., Awaitable[Any]]](
    method: str,
    request_model: type[CapabilityRequest],
    *,
    trace_queries: TraceHook | None = None,
    trace_input_count: TraceHook | None = None,
    trace_result_count: TraceHook | None = None,
) -> Callable[[HandlerT], HandlerT]:
    """Declare one RPC method on a capability module."""

    normalized_method = method.strip()
    if not normalized_method:
        raise ValueError("Capability method must not be empty")
    if not issubclass(request_model, CapabilityRequest):
        raise TypeError("Capability request model must inherit CapabilityRequest")
    for label, hook in (
        ("trace_queries", trace_queries),
        ("trace_input_count", trace_input_count),
        ("trace_result_count", trace_result_count),
    ):
        if hook is not None and not callable(hook) and not (isinstance(hook, str) and hook):
            raise TypeError(f"Capability {label} hook must be a callable or attribute name")

    def decorate(handler: HandlerT) -> HandlerT:
        if not inspect.iscoroutinefunction(handler):
            raise TypeError(f"Capability handler {handler.__name__!r} must be async")
        if hasattr(handler, _CAPABILITY_METHOD_ATTRIBUTE):
            raise ValueError(f"Capability handler {handler.__name__!r} is already registered")
        setattr(
            handler,
            _CAPABILITY_METHOD_ATTRIBUTE,
            CapabilityMethodDefinition(
                method=normalized_method,
                request_model=request_model,
                handler_name=handler.__name__,
                trace_queries=trace_queries,
                trace_input_count=trace_input_count,
                trace_result_count=trace_result_count,
            ),
        )
        return handler

    return decorate


class BaseCapabilities(ABC):
    """Base for capability modules discovered from explicitly loaded plugin modules."""

    name: ClassVar[str] = ""
    available = True

    @classmethod
    @abstractmethod
    def from_context(cls, context: Any) -> Self:
        """Build the module from broker-owned backend bindings and configuration."""

    def specs(self) -> tuple[CapabilitySpec, ...]:
        definitions: list[CapabilityMethodDefinition] = []
        attributes: dict[str, Any] = {}
        for module_type in reversed(type(self).__mro__):
            attributes.update(vars(module_type))
        for value in attributes.values():
            target = value.__func__ if isinstance(value, (classmethod, staticmethod)) else value
            definition = getattr(target, _CAPABILITY_METHOD_ATTRIBUTE, None)
            if isinstance(definition, CapabilityMethodDefinition):
                definitions.append(definition)

        methods: set[str] = set()
        specs: list[CapabilitySpec] = []
        for definition in definitions:
            if definition.method in methods:
                raise ValueError(
                    f"Capability module {self.name!r} declares duplicate method "
                    f"{definition.method!r}"
                )
            methods.add(definition.method)
            specs.append(
                CapabilitySpec(
                    method=definition.method,
                    request_model=definition.request_model,
                    handler=cast(
                        CapabilityHandler,
                        getattr(self, definition.handler_name),
                    ),
                    trace=TraceSpec(
                        queries=cast(
                            TraceQueries,
                            self._resolve_hook(definition.trace_queries) or _no_queries,
                        ),
                        input_count=cast(
                            TraceCount,
                            self._resolve_hook(definition.trace_input_count) or _one_input,
                        ),
                        result_count=cast(
                            ResultCount,
                            self._resolve_hook(definition.trace_result_count)
                            or _default_result_count,
                        ),
                    ),
                )
            )
        return tuple(specs)

    def manifest(self, *, backend_name: str) -> dict[str, Any] | None:
        del backend_name
        return None

    def _resolve_hook(self, hook: TraceHook | None) -> Callable[..., Any] | None:
        if hook is None:
            return None
        resolved = getattr(self, hook) if isinstance(hook, str) else hook
        if not callable(resolved):
            raise TypeError(f"Capability hook {hook!r} on module {self.name!r} is not callable")
        return resolved


def capability_types_from_module(module: ModuleType) -> tuple[type[BaseCapabilities], ...]:
    """Collect concrete capability classes declared in one explicitly loaded module."""

    module_types: list[type[BaseCapabilities]] = []
    seen: set[int] = set()
    for value in vars(module).values():
        if (
            not inspect.isclass(value)
            or value is BaseCapabilities
            or not issubclass(value, BaseCapabilities)
            or inspect.isabstract(value)
            or value.__module__ != module.__name__
        ):
            continue
        if id(value) in seen:
            continue
        seen.add(id(value))
        module_types.append(value)
    return tuple(module_types)


class CapabilityRegistry:
    def __init__(self, modules: Iterable[BaseCapabilities]) -> None:
        self._modules: dict[str, BaseCapabilities] = {}
        self._specs: dict[str, CapabilitySpec] = {}
        for module in modules:
            if module.name in self._modules:
                raise ValueError(f"Duplicate capability module: {module.name}")
            self._modules[module.name] = module
            specs = module.specs()
            if not specs:
                raise ValueError(f"Capability module {module.name!r} declares no methods")
            for spec in specs:
                if not isinstance(spec, CapabilitySpec):
                    raise TypeError(
                        f"Capability module {module.name!r} returned an invalid method spec"
                    )
                family = spec.method.partition(".")[0]
                if family != module.name:
                    raise ValueError(
                        f"Capability method {spec.method!r} must belong to module {module.name!r}"
                    )
                if spec.method in self._specs:
                    raise ValueError(f"Duplicate capability method: {spec.method}")
                self._specs[spec.method] = spec

    @property
    def module_names(self) -> tuple[str, ...]:
        return tuple(self._modules)

    @property
    def methods(self) -> tuple[str, ...]:
        return tuple(self._specs)

    def get(self, method: str) -> CapabilitySpec | None:
        return self._specs.get(method)

    @property
    def available_methods(self) -> tuple[str, ...]:
        return tuple(
            method for method in self._specs if self._modules[method.partition(".")[0]].available
        )

    def manifest(self, *, backend_name: str) -> dict[str, Any]:
        manifest: dict[str, Any] = {}
        for module in self._modules.values():
            section = module.manifest(backend_name=backend_name)
            if section is not None:
                manifest[module.name] = section
        return manifest
