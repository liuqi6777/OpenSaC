from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from opensac.broker.providers.execution import ProviderExecutor
from opensac.broker.providers.flights import ProviderFlightCoordinator
from opensac.broker.session import BrokerSession
from opensac.provider import ProviderRequestError, ProviderRuntime


class ServiceExecution:
    """Bind one backend and policy runtime without a string dispatch key."""

    component: str
    resource_failures = False

    def __init__(
        self,
        backend: Any,
        providers: ProviderExecutor,
        runtime: ProviderRuntime,
    ) -> None:
        self.backend = backend
        self.providers = providers
        self.runtime = runtime
        self._namespace = f"service:{uuid.uuid4().hex}"

    @property
    def flights(self) -> ProviderFlightCoordinator:
        return self.providers.flights

    @property
    def inflight_coalescing(self) -> bool:
        return self.flights.enabled

    def capacity_snapshot(self) -> dict[str, int]:
        return self.runtime.snapshot(self.providers.provider_identity(self.backend))

    def fingerprint(self, value: Any) -> str:
        return self.providers.fingerprint(value)

    def flight_key(self, request_fingerprint: str) -> str:
        return self.flights.key(self._namespace, request_fingerprint)

    def record_deduplicated_request(
        self,
        *,
        request_index: int,
        leader_index: int,
        request_fingerprint: str,
    ) -> None:
        self.providers.record_deduplicated_request(
            request_index=request_index,
            leader_index=leader_index,
            request_fingerprint=request_fingerprint,
        )

    def provider_failure(self, error: ProviderRequestError) -> dict[str, Any]:
        return self.providers.provider_failure(error)

    def contextualize_failure(self, failure: dict[str, Any]) -> dict[str, Any]:
        return self.providers.contextualize_failure(
            failure,
            backend=self.backend,
            component=self.component,
            resource_failures=self.resource_failures,
        )

    async def run(
        self,
        state: BrokerSession,
        *,
        request_indexes: list[int],
        request_value: Any,
        request: Callable[[], Awaitable[Any]],
        preflight: Callable[[], None] | None = None,
        request_id: str | None = None,
        track_execution: bool = True,
    ) -> Any:
        return await self.providers.run(
            state,
            runtime=self.runtime,
            backend=self.backend,
            component=self.component,
            namespace=self._namespace,
            resource_failures=self.resource_failures,
            request_indexes=request_indexes,
            request_value=request_value,
            request=request,
            preflight=preflight,
            request_id=request_id,
            track_execution=track_execution,
        )
