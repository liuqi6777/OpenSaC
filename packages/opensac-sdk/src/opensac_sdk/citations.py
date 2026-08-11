from __future__ import annotations

from typing import Any

from .models import CitationRequest
from .transport import UnixSocketTransport


class CitationsResource:
    """What the broker knows about a handle you already hold.

    The broker has always been able to answer this -- `output.submit` calls it
    to turn refs into trusted citations -- but it was reachable only from inside
    that one call, so "what document is this?" could be asked at the end of a
    program and nowhere else. Triage needs the same answer at the start.

    It is not a way to reach new documents: every handle must still have been
    returned by a search in this session, and unknown ones raise.
    """

    def __init__(self, transport: UnixSocketTransport) -> None:
        self._transport = transport

    def resolve(self, refs: list[str]) -> list[dict[str, Any]]:
        """Title, URL, docid, backend, and search evidence for each handle.

        Accepts a ref, a docid, or a URL, like every other consumer of a
        handle.
        """
        return self._transport.call("citations.resolve", {"refs": refs})

    def resolve_requests(
        self, requests: list[CitationRequest | dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Resolve passage locators, preserving request order and legacy refs."""
        parsed = [CitationRequest.model_validate(item) for item in requests]
        return self._transport.call(
            "citations.resolve",
            {"requests": [item.model_dump(exclude_none=True) for item in parsed]},
        )
