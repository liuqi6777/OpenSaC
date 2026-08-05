from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True)
class SandboxRequest:
    code: str
    workspace: Path
    session_token: str


@dataclass
class SandboxResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    output: Any = None
    citations: list[dict[str, Any]] = field(default_factory=list)
    timed_out: bool = False

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


class Sandbox(Protocol):
    async def execute(self, request: SandboxRequest) -> SandboxResult: ...
