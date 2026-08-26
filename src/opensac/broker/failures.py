"""Stable failure payloads shared by capability and RPC boundaries."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CapabilityFailure(BaseModel):
    """Sanitized failure diagnostics safe to expose across the broker boundary."""

    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    retryable: bool
    attempts: int = Field(default=0, ge=0)
    provider_status: int | None = Field(default=None, ge=100, le=599)
    retry_after_seconds: float | None = Field(default=None, ge=0.0)
    provider: str | None = None
    component: str | None = None
    scope: Literal["request", "resource", "provider", "unknown"] | None = None
