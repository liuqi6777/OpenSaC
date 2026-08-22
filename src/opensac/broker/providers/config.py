from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ProviderExecutionConfig:
    """Broker-owned coordination and result-cache settings."""

    inflight_coalescing: bool = False
    max_inflight_keys: int = 256
    max_waiters_per_flight: int = 64
    result_cache_ttl_seconds: float = 0.0
    result_cache_max_bytes: int = 128_000_000

    def __post_init__(self) -> None:
        if self.max_inflight_keys < 1:
            raise ValueError("max_inflight_keys must be at least one")
        if self.max_waiters_per_flight < 1:
            raise ValueError("max_waiters_per_flight must be at least one")
        if self.result_cache_ttl_seconds < 0:
            raise ValueError("provider result cache TTL cannot be negative")
        if self.result_cache_max_bytes < 1:
            raise ValueError("provider result cache max bytes must be at least one")
