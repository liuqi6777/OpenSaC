from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Self

from opensac.broker.registry import BaseCapabilities, EmptyRequest, capability_method
from opensac.broker.session import BrokerSession

if TYPE_CHECKING:
    from opensac.broker.capabilities.catalog import CapabilityBuildContext

type SessionManifest = Callable[[BrokerSession], dict[str, Any]]


class SessionCapabilities(BaseCapabilities):
    """Expose broker-owned session state and capability negotiation."""

    name = "session"
    available = True

    def __init__(self, manifest_for_state: SessionManifest) -> None:
        self._manifest_for_state = manifest_for_state

    @classmethod
    def from_context(cls, context: CapabilityBuildContext) -> Self:
        return cls(context.session_manifest)

    @capability_method("session.capabilities", EmptyRequest)
    async def capabilities(
        self,
        state: BrokerSession,
        _request: EmptyRequest,
    ) -> dict[str, Any]:
        return self._manifest_for_state(state)
