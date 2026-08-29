from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class SurfaceTier(StrEnum):
    CORE = "core"
    HELPER = "helper"
    ADVANCED = "advanced"
    INTERNAL = "internal"


@dataclass(frozen=True, slots=True)
class OperationSpec:
    resource: str
    method: str
    tier: SurfaceTier
    transport_method: str | None = None
    model_core: bool = False

    @property
    def public_name(self) -> str:
        if self.method == "__call__":
            return f"sdk.{self.resource}"
        return f"sdk.{self.resource}.{self.method}"


SDK_SURFACE: tuple[OperationSpec, ...] = (
    OperationSpec("search", "__call__", SurfaceTier.CORE, "search.query", model_core=True),
    OperationSpec("search", "many", SurfaceTier.CORE, "search.query_many", model_core=True),
    OperationSpec("search", "fuse_rrf", SurfaceTier.HELPER, model_core=True),
    OperationSpec("content", "fetch", SurfaceTier.CORE, "content.fetch", model_core=True),
    OperationSpec(
        "content",
        "passages",
        SurfaceTier.CORE,
        "content.passages",
        model_core=True,
    ),
    OperationSpec("content", "read", SurfaceTier.CORE, "content.read", model_core=True),
    OperationSpec(
        "content",
        "grep",
        SurfaceTier.CORE,
        "content.grep",
        model_core=True,
    ),
    OperationSpec("session", "usage", SurfaceTier.CORE, "session.usage", model_core=True),
    OperationSpec(
        "session",
        "capabilities",
        SurfaceTier.CORE,
        "session.capabilities",
        model_core=True,
    ),
    OperationSpec("state", "write_jsonl", SurfaceTier.HELPER),
    OperationSpec("state", "append_jsonl", SurfaceTier.HELPER),
    OperationSpec("state", "upsert_jsonl", SurfaceTier.HELPER),
    OperationSpec("state", "exists", SurfaceTier.HELPER),
    OperationSpec("state", "list", SurfaceTier.HELPER),
    OperationSpec("state", "read_jsonl", SurfaceTier.HELPER),
    OperationSpec("state", "write_json", SurfaceTier.HELPER),
    OperationSpec("state", "read_json", SurfaceTier.HELPER),
    OperationSpec("state", "from_environment", SurfaceTier.INTERNAL),
    OperationSpec("output", "submit", SurfaceTier.CORE, model_core=True),
    OperationSpec("output", "from_environment", SurfaceTier.INTERNAL),
    OperationSpec("llm", "complete", SurfaceTier.ADVANCED, "llm.complete"),
    OperationSpec(
        "llm",
        "extract",
        SurfaceTier.CORE,
        "llm.extract",
        model_core=True,
    ),
)

MODEL_CORE_METHODS: tuple[str, ...] = tuple(
    operation.public_name for operation in SDK_SURFACE if operation.model_core
)

SDK_PUBLIC_OPERATIONS: tuple[str, ...] = tuple(
    operation.public_name for operation in SDK_SURFACE if operation.tier is not SurfaceTier.INTERNAL
)

BROKER_METHODS: tuple[str, ...] = tuple(
    dict.fromkeys(
        operation.transport_method
        for operation in SDK_SURFACE
        if operation.transport_method is not None
    )
)
