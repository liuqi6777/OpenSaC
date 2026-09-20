"""Capability provider contract and discovery through installed Python entry points.

A provider is a synchronous factory (config, context) returning an object whose
capability operation and aclose are async. Construction must not perform network I/O.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import Any, Literal, Protocol, overload

from pydantic import BaseModel, Field, HttpUrl, SecretStr

from .config import ModelName, Settings
from .contracts import Completion, ConfigurationCheck, Document, RerankResult, SearchHit
from .errors import ConfigurationError, ErrorInfo


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
async def load_provider(
    capability: Literal["rerank"], name: str, config: ProviderConfig, context: ProviderContext
) -> RerankProvider: ...


@overload
async def load_provider(
    capability: Literal["llm"], name: str, config: ProviderConfig, context: ProviderContext
) -> LLMProvider: ...


@overload
async def load_provider(
    capability: Literal["search"], name: str, config: ProviderConfig, context: ProviderContext
) -> SearchProvider: ...


@overload
async def load_provider(
    capability: Literal["fetch"], name: str, config: ProviderConfig, context: ProviderContext
) -> FetchProvider: ...


async def load_provider(
    capability: str, name: str, config: ProviderConfig, context: ProviderContext
) -> Any:
    factory = provider_factory(capability, name)
    provider = factory(config, context)
    if inspect.isawaitable(provider):
        if inspect.iscoroutine(provider):
            provider.close()
        raise ConfigurationError("Provider factories must be synchronous.")
    try:
        for method in ("complete" if capability == "llm" else capability, "aclose"):
            if not inspect.iscoroutinefunction(getattr(provider, method, None)):
                raise TypeError(f"Provider {name!r} must implement async {method}().")
    except Exception:
        close = getattr(provider, "aclose", None)
        if inspect.iscoroutinefunction(close):
            # Finish disposal before releasing the failed factory's only owned instance.
            cleanup = asyncio.create_task(close())
            cancelled = False
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    cancelled = True
                except Exception:
                    break
            if cancelled:
                # Retrieve a cleanup failure before propagating caller cancellation.
                if not cleanup.cancelled():
                    cleanup.exception()
                raise asyncio.CancelledError from None
            cleanup.result()
        raise
    return provider


def provider_factory(capability: str, name: str) -> Any:
    """Discover and import a factory without constructing provider resources."""
    group = f"opensac.{capability}"
    candidates = list(entry_points(group=group, name=name))
    if len(candidates) != 1:
        reason = "not installed" if not candidates else "registered by multiple distributions"
        raise ConfigurationError(f"Provider for {capability} is {reason}.")
    factory = candidates[0].load()
    if not callable(factory) or inspect.iscoroutinefunction(factory):
        raise ConfigurationError(f"Provider for {capability} must expose a callable factory.")
    return factory


def provider_config(settings: Settings, capability: str) -> ProviderConfig:
    return ProviderConfig(
        base_url=getattr(settings, f"{capability}_base_url"),
        api_key=getattr(settings, f"{capability}_api_key"),
        model=getattr(settings, f"{capability}_model", None),
        options=getattr(settings, f"{capability}_options"),
        max_tokens=settings.llm_max_tokens if capability == "llm" else 1024,
        temperature=settings.llm_temperature if capability == "llm" else None,
    ).model_copy(deep=True)


def check_configuration(
    settings: Settings, *, injected: frozenset[str] = frozenset()
) -> list[ConfigurationCheck]:
    """Run optional synchronous, offline factory validators; never instantiate providers."""
    checks = []
    for capability in ("search", "fetch", "rerank", "llm"):
        name = getattr(settings, f"{capability}_provider")
        if capability in injected:
            checks.append(
                ConfigurationCheck(capability=capability, provider="injected", status="unverified")
            )
            continue
        try:
            factory = provider_factory(capability, name)
            validator = getattr(factory, "validate_config", None)
            if validator is None:
                checks.append(
                    ConfigurationCheck(capability=capability, provider=name, status="unverified")
                )
                continue
            if not callable(validator) or inspect.iscoroutinefunction(validator):
                raise ConfigurationError("Provider configuration validator must be synchronous.")
            result = validator(provider_config(settings, capability))
            if inspect.isawaitable(result):
                if inspect.iscoroutine(result):
                    result.close()
                raise ConfigurationError("Provider configuration validator must be synchronous.")
        except ConfigurationError as exc:
            error = ErrorInfo(code=exc.code, message=exc.message, retryable=False)
        except Exception:
            error = ErrorInfo(
                code="configuration_error",
                message="Provider discovery or validation failed.",
                retryable=False,
            )
        else:
            checks.append(ConfigurationCheck(capability=capability, provider=name, status="passed"))
            continue
        checks.append(
            ConfigurationCheck(capability=capability, provider=name, status="failed", error=error)
        )
    return checks


class ManagedProvider(Protocol):
    async def aclose(self) -> None: ...


class ProviderSlot[T: ManagedProvider]:
    """Own one lazy, capability-specific provider and its initialization failure."""

    def __init__(self, factory: Callable[[], Awaitable[T]], instance: T | None = None) -> None:
        self._factory = factory
        self._instance = instance
        self._failed = False
        self._lock = asyncio.Lock()

    async def get(self) -> T:
        async with self._lock:
            if self._failed:
                raise ConfigurationError("Capability provider could not initialize.")
            if self._instance is None:
                try:
                    self._instance = await self._factory()
                except BaseException as exc:
                    self._failed = True
                    if not isinstance(exc, Exception):
                        raise
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
        self.injected = frozenset(
            capability
            for capability, instance in (
                ("search", search),
                ("fetch", fetch),
                ("rerank", rerank),
                ("llm", llm),
            )
            if instance is not None
        )

        def config(capability: str) -> ProviderConfig:
            return provider_config(settings, capability)

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
        failures: list[BaseException] = []
        for slot in (self.search, self.fetch, self.rerank, self.llm):
            provider = slot.instance
            if provider is None or id(provider) in seen:
                continue
            seen.add(id(provider))
            try:
                await provider.aclose()
            except (Exception, asyncio.CancelledError) as exc:
                failures.append(exc)
        if failures:
            raise BaseExceptionGroup("Provider cleanup failed", failures)
