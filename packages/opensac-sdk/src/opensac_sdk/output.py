from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .models import SubmittedOutput
from .transport import UnixSocketTransport


class OutputResource:
    def __init__(self, output_path: str, transport: UnixSocketTransport | None = None) -> None:
        self._output_path = Path(output_path)
        self._transport = transport

    def submit(self, output: Any, *, citations: list[dict[str, Any]] | None = None) -> None:
        requested = citations or []
        if requested:
            if self._transport is None:
                raise RuntimeError("Citation resolution requires a broker transport")
            refs = [str(citation.get("ref", "")) for citation in requested]
            if any(not ref for ref in refs):
                raise ValueError("Every citation must contain a search result ref")
            resolved = self._transport.call("citations.resolve", {"refs": refs})
        else:
            resolved = []
        payload = SubmittedOutput(output=output, citations=resolved)
        self._output_path.write_text(
            json.dumps(payload.model_dump(), ensure_ascii=True, indent=2, default=str),
            encoding="utf-8",
        )

    @classmethod
    def from_environment(cls, transport: UnixSocketTransport | None = None) -> OutputResource:
        return cls(
            os.environ.get("OPENSAC_OUTPUT_PATH", "/workspace/.opensac-output.json"),
            transport,
        )
