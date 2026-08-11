from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .models import CitationRequest, SubmittedOutput
from .transport import UnixSocketTransport


class OutputResource:
    def __init__(self, output_path: str, transport: UnixSocketTransport | None = None) -> None:
        self._output_path = Path(output_path)
        self._transport = transport

    def submit(
        self,
        output: Any,
        *,
        citations: list[CitationRequest | dict[str, Any]] | None = None,
    ) -> None:
        requested = [CitationRequest.model_validate(item) for item in citations or []]
        if requested:
            if self._transport is None:
                raise RuntimeError("Citation resolution requires a broker transport")
            if any(not citation.ref for citation in requested):
                raise ValueError("Every citation must contain a search result ref")
            if any(citation.locator is not None for citation in requested):
                resolved = self._transport.call(
                    "citations.resolve",
                    {
                        "requests": [
                            citation.model_dump(exclude_none=True) for citation in requested
                        ]
                    },
                )
            else:
                resolved = self._transport.call(
                    "citations.resolve", {"refs": [citation.ref for citation in requested]}
                )
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
