from __future__ import annotations

from typing import Any

from .transport import UnixSocketTransport


class SessionResource:
    """What this rollout has spent so far.

    The counters exist on the host and are rendered into the observation the
    control model reads, but the program that decides whether to search again
    runs in the sandbox and could not see them. Asking a model to ration a
    budget its code cannot read is asking it to guess.
    """

    def __init__(self, transport: UnixSocketTransport) -> None:
        self._transport = transport

    def usage(self) -> dict[str, Any]:
        """Spend and ceilings together.

        Returns `search_calls`, `llm_calls`, `content_fetches`,
        `content_backend_fetches`, `pipeline_model_tokens`, `sandbox_seconds`,
        `documents_seen`, and the `max_search_calls` / `max_llm_calls` they are
        measured against.
        """
        return self._transport.call("session.usage", {})
