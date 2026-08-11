from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

_MISSING = object()


class SubscriptableModel(BaseModel):
    """A result object that also answers to the access style of a mapping.

    Generated programs reach for ``hit["docid"]`` and ``snippet.get("text")``
    constantly, because almost every search API a model has ever read returns
    dictionaries. That prior does not go away when a docstring says otherwise,
    and the failure is not a graceful one: ``'SearchHit' object is not
    subscriptable`` aborts the program and costs the whole turn.

    This is not two representations tolerating each other. There is one type;
    it accepts two spellings of the same field read. Nothing here can reach a
    field the attribute form could not, so the shape of a result is unchanged
    and the two spellings can never disagree.

    Writes stay attribute-only on purpose. ``hit["ref"] = ...`` would be a
    program editing its own handle, and a handle it has edited is one the
    broker will refuse.
    """

    def __getitem__(self, key: str) -> Any:
        value = getattr(self, key, _MISSING)
        if value is _MISSING:
            # KeyError rather than AttributeError: the caller asked in the
            # mapping style and is likely wrapped in the matching except.
            raise KeyError(key)
        return value

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and key in type(self).model_fields

    def keys(self) -> Any:
        # Present so `dict(hit)` and `{**hit}` work, which is the third way a
        # program tries to treat a result as a mapping.
        return type(self).model_fields.keys()


class SearchHit(SubscriptableModel):
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


class SearchBatch(SubscriptableModel):
    query: str
    hits: list[SearchHit] = Field(default_factory=list)
    error: str | None = None


class ContentSnippet(SubscriptableModel):
    ref: str
    text: str
    url: str | None = None
    title: str = ""
    # Carried over from the hit this text came from, for the same reason
    # `SearchHit.date` exists. Without it a program that filters on time has to
    # keep the hits alongside the snippets and join them by ref, and the shape
    # of the SDK is what suggests otherwise: a snippet that has `title` and
    # `url` but no `date` reads like an oversight, and a program written on
    # that assumption dies on `AttributeError` rather than missing a filter.
    date: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ContentMatch(SubscriptableModel):
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
