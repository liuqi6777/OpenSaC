from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True)
class SandboxRequest:
    code: str
    workspace: Path
    session_token: str
    execution_id: str | None = None


@dataclass
class SandboxResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    output: Any = None
    citations: list[dict[str, Any]] = field(default_factory=list)
    timed_out: bool = False
    # Set when the runtime refused to start the program at all: a missing
    # image, an unavailable daemon, a host that rejects a resource flag. Kept
    # apart from stderr because a control model can act on a failing program
    # and cannot act on this, and because an operator reading a transcript
    # needs to tell "the model could not solve it" from "nothing ever ran".
    launch_error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and self.launch_error is None


class Sandbox(Protocol):
    async def execute(self, request: SandboxRequest) -> SandboxResult: ...
