from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class SearchHit(BaseModel):
    ref: str
    backend: str
    title: str = ""
    url: str | None = None
    docid: str | None = None
    domain: str | None = None
    snippet: str = ""
    score: float | None = None
    rank: int
    metadata: dict[str, Any] = Field(default_factory=dict)


class SearchBatch(BaseModel):
    query: str
    hits: list[SearchHit] = Field(default_factory=list)
    error: str | None = None


class ContentSnippet(BaseModel):
    ref: str
    text: str
    url: str | None = None
    title: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class RpcRequest(BaseModel):
    method: str
    params: dict[str, Any] = Field(default_factory=dict)


class RpcResponse(BaseModel):
    ok: bool
    result: Any = None
    error: str | None = None


class SubmittedOutput(BaseModel):
    output: Any
    citations: list[dict[str, Any]] = Field(default_factory=list)


class SandboxEvent(BaseModel):
    type: Literal["output", "log"]
    payload: Any
