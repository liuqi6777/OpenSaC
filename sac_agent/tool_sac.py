"""The single tool exposed by the minimal Search-as-Code agent."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any

import httpx

from opensac.sac_run import (
    DEFAULT_TIMEOUT_SECONDS,
    AsyncSessionClient,
    render_observation,
    truncate_observation,
)


@dataclass(frozen=True)
class SacConfig:
    api_base: str = "http://127.0.0.1:8000"
    api_key: str = ""

    @classmethod
    def from_env(cls) -> SacConfig:
        return cls(
            api_base=os.getenv("SAC_API_BASE", "http://127.0.0.1:8000").rstrip("/"),
            api_key=os.getenv("SAC_API_KEY") or os.getenv("OPENSAC_API_KEY") or "",
        )


_SKILL = """# Search as Code

Your only tool is `sac_run(code)`. It runs Python in a networkless sandbox with
`opensac_sdk` preinstalled. Begin each program with:

```python
from opensac_sdk import BrokerError, sdk
```

Core primitives:

- `sdk.search(query, limit=10, offset=0)`
- `sdk.search.many(queries, limit_per_query=10, offset=0, concurrency=5)`
- `sdk.search.fuse_rrf(batches, weights=None, k=60, limit=None)`
- `sdk.content.grep_report(refs, pattern, context=0, max_matches_per_ref=20)`
- `sdk.content.read(refs, offset=1, limit=200, max_chars=100000)`
- `sdk.content.get_many(refs)` and `sdk.content.snippets(query, refs, ...)`
- `sdk.llm.complete(...)` and `sdk.llm.extract_many(...)`
- `sdk.state.merge_jsonl/read_jsonl/write_json/read_json/exists/list`
- `sdk.output.submit(output, citations=[{"ref": ref, "locator": locator}])`

Search returns opaque refs; never invent or edit them. Search snippets are previews, not final
evidence. Read the content used for the answer. `grep_report` distinguishes no match from fetch
failure; `read` offsets are 1-indexed. Cite the exact locator returned with a non-empty passage.
Keep refs and locators lossless and reuse them only inside this live agent run.

Make one program carry a full research stage: fan out 6-12 queries, fuse/deduplicate results,
filter in ordinary Python, grep the pool, then read a few useful passages. Save a bounded pool
and evidence ledger with `sdk.state`; files and refs survive across `sac_run` calls, but Python
variables do not. Print or submit only compact conclusions because raw capability results remain
inside the sandbox. Catch `BrokerError`, inspect typed `.failure` values, and make progress instead
of repeating a failed call. Use `sdk.llm.extract_many` only for semantic work needing a checked
JSON shape; use normal Python for regex, joins, ranking, counts, and coverage.

When every constraint has verified evidence, stop calling the tool and answer the user. Return
the final answer directly as your entire response, without wrapper tags or a preamble.
"""


class SacRunTool:
    """One lazily-created OpenSAC session, reused until the rollout ends."""

    name = "sac_run"

    def __init__(
        self,
        config: SacConfig | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config = config or SacConfig.from_env()
        self._transport = transport
        self._client: AsyncSessionClient | None = None
        self._session_id: str | None = None
        self._session_lock = asyncio.Lock()
        self.close_error: str | None = None

    @property
    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": "Run a Python research program in the persistent OpenSAC sandbox.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "code": {
                            "type": "string",
                            "description": "Python source using the preinstalled opensac_sdk.",
                        }
                    },
                    "required": ["code"],
                    "additionalProperties": False,
                },
            },
        }

    @property
    def system_prompt_addendum(self) -> str:
        return _SKILL

    def _session_client(self) -> AsyncSessionClient:
        if self._client is None:
            self._client = AsyncSessionClient(
                api_base=self.config.api_base,
                api_key=self.config.api_key,
                timeout=DEFAULT_TIMEOUT_SECONDS,
                transport=self._transport,
            )
        return self._client

    def _http(self) -> httpx.AsyncClient:
        """Retain the minimal agent's existing test/debug access to its HTTP client."""
        return self._session_client().http

    async def _ensure_session(self) -> str:
        if self._session_id is not None:
            return self._session_id
        async with self._session_lock:
            if self._session_id is None:
                session = await self._session_client().create_session()
                self._session_id = str(session["id"])
        return self._session_id

    async def call(self, arguments: dict[str, Any]) -> str:
        code = arguments.get("code")
        if not isinstance(code, str) or not code.strip():
            return "[sac_run] Expected a non-empty string in the 'code' field."

        try:
            session_id = await self._ensure_session()
            payload = await self._session_client().exec_code(
                session_id,
                code,
                include_trace=False,
            )
            return self._render(payload)
        except httpx.TimeoutException:
            return f"[sac_run] Timed out after {DEFAULT_TIMEOUT_SECONDS:.0f}s."
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            return f"[sac_run] OpenSAC request failed: {type(exc).__name__}: {exc}"

    def _render(self, payload: dict[str, Any]) -> str:
        return render_observation(payload)

    @staticmethod
    def _truncate(text: str, limit: int) -> str:
        return truncate_observation(text, limit)

    async def aclose(self) -> None:
        try:
            if self._session_id is not None and self._client is not None:
                await self._client.delete_session(self._session_id)
        except Exception as exc:
            # Cleanup must not replace an answer the rollout already produced.
            self.close_error = f"{type(exc).__name__}: {exc}"
        finally:
            self._session_id = None
            if self._client is not None:
                await self._client.close()
                self._client = None
