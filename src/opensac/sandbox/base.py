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
    # Per-execution names for the two files the runtime puts in the workspace.
    # They used to be fixed, which meant two `/exec` calls sharing a session
    # overwrote each other's program and output: whichever container started
    # last decided what both of them ran. The archived copy of a program has to
    # be the one that actually executed, so this is a correctness requirement
    # for the program corpus and not only a concurrency fix. Defaults preserve
    # the historical names for callers that construct a request positionally.
    program_filename: str = ".opensac-program.py"
    output_filename: str = ".opensac-output.json"
    # The broker token is an execution credential, not a stable session key:
    # internal runs mint a fresh token even when they share one session.  Warm
    # sandboxes therefore use this id for lifecycle/reuse when the caller can
    # provide it, and fall back to the token for older callers.
    session_id: str | None = None
    # A session budget may lower the deployment-wide timeout for this call.
    timeout_seconds: float | None = None


@dataclass
class SandboxResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    output: Any = None
    citations: list[str] = field(default_factory=list)
    timed_out: bool = False
    # Host-observed phase timings. Optional so alternate sandbox
    # implementations keep their existing contract.
    timings: dict[str, float] = field(default_factory=dict)
    # Set when the runtime refused to start the program at all: a missing
    # image, an unavailable daemon, a host that rejects a resource flag. Kept
    # apart from stderr because a control model can act on a failing program
    # and cannot act on this, and because an operator reading a transcript
    # needs to tell "the model could not solve it" from "nothing ever ran".
    launch_error: str | None = None
    # Appended to preserve the positional constructor contract for existing
    # alternate sandbox implementations.
    output_limit_exceeded: bool = False
    warnings: list[dict[str, Any]] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return (
            self.exit_code == 0
            and not self.timed_out
            and not self.output_limit_exceeded
            and self.launch_error is None
        )


class Sandbox(Protocol):
    async def execute(self, request: SandboxRequest) -> SandboxResult: ...
