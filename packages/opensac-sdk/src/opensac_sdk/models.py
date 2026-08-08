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
    # Publication date as the backend reports it, un-normalised. First class
    # rather than a `metadata` key because a large share of retrieval tasks
    # constrain time ("released between 1980 and 2000", "as of 2023"), and a
    # program can only filter on a field it can guess the name of. Anything
    # else the backend knows stays in `metadata`.
    date: str | None = None
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


class ContentMatch(BaseModel):
    """One line of a document that matched a pattern, with its coordinates.

    ``line`` is 1-indexed and is the same coordinate ``content.read`` takes as
    ``offset``, so locating and reading compose without arithmetic:

        matches = sdk.content.grep(refs, r"born in \\d{4}")
        window = sdk.content.read([matches[0].ref], offset=matches[0].line - 5)
    """

    ref: str
    docid: str | None = None
    url: str | None = None
    title: str = ""
    line: int
    text: str
    # Empty unless `context` was requested. Kept as two lists rather than one
    # so a program can tell which side of the match a line came from.
    before: list[str] = Field(default_factory=list)
    after: list[str] = Field(default_factory=list)


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
