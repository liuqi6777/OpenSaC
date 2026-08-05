from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(UTC)


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RunLimits(BaseModel):
    max_turns: int = Field(default=8, ge=1, le=50)
    max_search_calls: int = Field(default=200, ge=1, le=5000)
    max_llm_calls: int = Field(default=30, ge=0, le=500)
    timeout_seconds: int = Field(default=300, ge=1, le=3600)


class SessionCreate(BaseModel):
    backends: list[str] = Field(default_factory=lambda: ["web", "local"])
    limits: RunLimits = Field(default_factory=RunLimits)


class Session(BaseModel):
    id: str
    token: str
    backends: list[str]
    limits: RunLimits
    workspace: str
    created_at: datetime = Field(default_factory=utc_now)


class RunCreate(BaseModel):
    input: str = Field(min_length=1)
    model: str | None = None
    output_schema: dict[str, Any] | None = None
    include_trace: bool = False


class RunUsage(BaseModel):
    model_tokens: int = 0
    search_calls: int = 0
    llm_calls: int = 0
    sandbox_seconds: float = 0.0


class Run(BaseModel):
    id: str
    session_id: str
    status: RunStatus = RunStatus.QUEUED
    input: str
    model: str | None = None
    output_schema: dict[str, Any] | None = None
    include_trace: bool = False
    output: Any = None
    citations: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None
    usage: RunUsage = Field(default_factory=RunUsage)
    trace: list[dict[str, Any]] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class PublicSession(BaseModel):
    id: str
    backends: list[str]
    limits: RunLimits
    created_at: datetime


class PublicRun(BaseModel):
    id: str
    session_id: str
    status: RunStatus
    input: str
    output: Any = None
    citations: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None
    usage: RunUsage
    trace: list[dict[str, Any]] | None = None
    artifacts: list[str] = Field(default_factory=list)
    events_url: str
    created_at: datetime
    updated_at: datetime
