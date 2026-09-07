"""Capability provider contract and discovery through installed Python entry points.

A provider is a synchronous factory (config, context) returning an object whose
capability operation and aclose are async. Construction must not perform network I/O.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import Any, Literal, Protocol, overload

from pydantic import BaseModel, Field, HttpUrl, SecretStr

from .config import ModelName, Settings
from .contracts import Completion, Document, RerankResult, SearchHit
from .errors import ConfigurationError


class ProviderConfig(BaseModel):
    model: ModelName | None = None
    max_tokens: int = Field(default=1024, ge=1, le=65536, strict=True)
    temperature: float | None = Field(default=None, ge=0, le=2, allow_inf_nan=False)
    base_url: HttpUrl | None = None
    api_key: SecretStr = SecretStr("")
    options: dict[str, Any] = Field(default_factory=dict)


@dataclass(frozen=True)
class ProviderContext:
    request_timeout: float = 30
    max_response_bytes: int = 2_000_000


class SearchProvider(Protocol):
    async def search(self, query: str, limit: int) -> list[SearchHit]: ...
    async def aclose(self) -> None: ...


class FetchProvider(Protocol):
    async def fetch(self, url: str) -> Document: ...
    async def aclose(self) -> None: ...


class RerankProvider(Protocol):
    async def rerank(
        self,
        query: str,
        documents: list[str],
        *,
        top_n: int | None = None,
    ) -> list[RerankResult]: ...
    async def aclose(self) -> None: ...


class LLMProvider(Protocol):
    async def complete(
        self, prompt: str, *, schema: dict[str, Any] | None = None
    ) -> Completion: ...
    async def aclose(self) -> None: ...


@overload
def load_provider(
    capability: Literal["rerank"], name: str, config: ProviderConfig, context: ProviderContext
) -> RerankProvider: ...


@overload
def load_provider(
    capability: Literal["llm"], name: str, config: ProviderConfig, context: ProviderContext
) -> LLMProvider: ...


@overload
def load_provider(
    capability: Literal["search"], name: str, config: ProviderConfig, context: ProviderContext
) -> SearchProvider: ...


@overload
def load_provider(
    capability: Literal["fetch"], name: str, config: ProviderConfig, context: ProviderContext
) -> FetchProvider: ...


def load_provider(
    capability: str, name: str, config: ProviderConfig, context: ProviderContext
) -> Any:
    group = f"opensac.{capability}"
    candidates = list(entry_points(group=group, name=name))
    if len(candidates) != 1:
        reason = "not installed" if not candidates else "registered by multiple distributions"
        raise ValueError(f"Provider {name!r} in {group!r} is {reason}.")
    factory = candidates[0].load()
    if not callable(factory):
        raise TypeError(f"Provider {name!r} must expose a callable factory.")
    provider = factory(config, context)
    for method in ("complete" if capability == "llm" else capability, "aclose"):
        if not inspect.iscoroutinefunction(getattr(provider, method, None)):
            raise TypeError(f"Provider {name!r} must implement async {method}().")
    return provider


class ManagedProvider(Protocol):
    async def aclose(self) -> None: ...


class ProviderSlot[T: ManagedProvider]:
    """Own one lazy, capability-specific provider and its initialization failure."""

    def __init__(self, factory: Callable[[], T], instance: T | None = None) -> None:
        self._factory = factory
        self._instance = instance
        self._failed = False

    def get(self) -> T:
        if self._failed:
            raise ConfigurationError("Capability provider could not initialize.")
        if self._instance is None:
            try:
                self._instance = self._factory()
            except Exception as exc:
                self._failed = True
                raise ConfigurationError("Capability provider could not initialize.") from exc
        return self._instance

    @property
    def instance(self) -> T | None:
        return self._instance


class Providers:
    """Typed capability slots with shared configuration and cleanup ownership."""

    def __init__(
        self,
        settings: Settings,
        context: ProviderContext,
        *,
        search: SearchProvider | None = None,
        fetch: FetchProvider | None = None,
        rerank: RerankProvider | None = None,
        llm: LLMProvider | None = None,
    ) -> None:
        def config(capability: str) -> ProviderConfig:
            return ProviderConfig(
                base_url=getattr(settings, f"{capability}_base_url"),
                api_key=getattr(settings, f"{capability}_api_key"),
                model=getattr(settings, f"{capability}_model", None),
                options=getattr(settings, f"{capability}_options"),
                max_tokens=settings.llm_max_tokens if capability == "llm" else 1024,
                temperature=settings.llm_temperature if capability == "llm" else None,
            )

        self.search = ProviderSlot(
            lambda: load_provider("search", settings.search_provider, config("search"), context),
            search,
        )
        self.fetch = ProviderSlot(
            lambda: load_provider("fetch", settings.fetch_provider, config("fetch"), context), fetch
        )
        self.rerank = ProviderSlot(
            lambda: load_provider("rerank", settings.rerank_provider, config("rerank"), context),
            rerank,
        )
        self.llm = ProviderSlot(
            lambda: load_provider("llm", settings.llm_provider, config("llm"), context), llm
        )

    async def aclose(self) -> None:
        seen: set[int] = set()
        failures: list[Exception] = []
        for slot in (self.search, self.fetch, self.rerank, self.llm):
            provider = slot.instance
            if provider is None or id(provider) in seen:
                continue
            seen.add(id(provider))
            try:
                await provider.aclose()
            except Exception as exc:
                failures.append(exc)
        if failures:
            raise ExceptionGroup("Provider cleanup failed", failures)
