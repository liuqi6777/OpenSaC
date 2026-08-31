"""The single tool exposed by the minimal Search-as-Code agent."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any

import httpx

from opensac.agent.sac_run import (
    DEFAULT_TIMEOUT_SECONDS,
    AsyncSessionClient,
    render_error,
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

Your only tool is `sac_run(code)`. It runs one complete Python research stage in a networkless
sandbox with `opensac_sdk` preinstalled. Use the SDK only; do not call REST APIs or manage sessions.
Begin programs with:

```python
from opensac_sdk import sdk
```

Core SDK surface:

- Search with `sdk.search.many(...)`; combine its aligned results with
  `sdk.search.fuse_rrf(queries, results, ...)`.
- Inspect documents with `sdk.content.grep(...)` and `sdk.content.read(...)`; read lines are
  1-based, character positions are 0-based, and `window.next` continues losslessly.
- Use single-item `sdk.llm.extract(...)` only for bounded semantic transformation.
- Persist artifacts with `sdk.workspace`; inspect deployment limits with `sdk.capabilities()`.
- Return only bounded stdout, carrying exact source URLs or local IDs beside the evidence they
  support.

## Work in deliberate stages

Frame the requested claims and source policy first. For each tool call, write one short, bounded
program.

- Continue inside the same program when an explicit Python rule can choose the next input. It is
  good to search, fuse, filter, grep, and read together when those transitions are mechanical.
- Stop when choosing the next query, source, pattern, or rule requires language judgment. Print at
  most eight useful summaries with exact sources and end with one `NEXT:` line naming that decision.
- A search-only stage is valid. Do not append grep merely for completeness.

## Tiny examples

When search results need interpretation, stop after a bounded preview:

```python
queries = ['"exact phrase" entity', "entity alternate wording"]
search_results = sdk.search.many(queries, limit=5)
for item in sdk.search.fuse_rrf(queries, search_results)[:5]:
    print(f"CANDIDATE source={item.source!r} title={item.title!r}")
print("NEXT: choose sources and checks")
```

For a one-claim task with known inputs, keep mechanical verification together:

```python
import re

sources = ["selected-source-url"]
pattern = r"target phrase"
results = sdk.content.grep(pattern, sources=sources, context_lines=2)
passage = None
for result in results:
    if result is None or not result.matches:
        continue
    match = result.matches[0]
    item = sdk.content.read(
        result.source, start_line=max(match.line - 8, 1), line_count=30, max_chars=12_000
    )
    if item is None:
        continue
    if re.search(pattern, item.text, re.IGNORECASE):
        passage = item
        break

if passage is None:
    print("NEXT: revise sources or pattern")
else:
    excerpt = " ".join(passage.text.split())[:1000]
    print(f"EVIDENCE source={passage.source!r} text={excerpt!r}")
    print("READY: synthesize the user-facing answer")
```

Use bounded comprehensions, `filter`, dicts, sets, `sorted`, `any`, and `all` to generate queries,
join by source, rank candidates, and measure coverage. Prefer `re`, dates, strings, and arithmetic
to an extraction call. `extract` cannot call tools, create trusted sources, or certify citation
labels. Validate its quoted evidence, clean and cap proposed follow-up inputs, then make bounded
SDK calls. Use `extract_many` for repeated extraction and branch on each aligned result.

## Keep the evidence boundary intact

- Pass URL/local-ID strings, never result records, to content. Public web URLs can be read directly
  and reused across runs; local IDs remain search-admitted only.
- Search metadata and snippets are for triage, or for a requested discovery list; they do not
  support claims about document content.
- For every material document-content claim, inspect non-empty text and carry its exact source
  string beside the printed evidence. Prefer primary sources and corroborate disputed claims.
- `sac_run` renders structured failure warnings automatically. Broker-backed single-item methods
  return a result or `None`; fan-out methods return an input-aligned list with `None` in failed
  positions. Check `is None`, never truthiness, because empty lists, strings, and objects can be
  successful results. Do not add `try/except` or print failures merely to expose them.

## End each stage deliberately

- `print` is the program result channel. Keep it bounded, avoid raw result objects and whole pages,
  and end review stages with `NEXT:` plus the unresolved decision.
- When stdout contains sufficient source-scoped evidence and no unresolved `NEXT:`, stop calling
  `sac_run` and answer the user directly. A separate finalization program is unnecessary.
- Agent completion is the final response to the user, not a special SDK call.

## Use workspace only when observation handoff is insufficient

Default to bounded stdout handoff. Even an Explore then Verify flow can remain stateless when the
chosen sources and checks fit safely in one observation. Passing five selected sources to the next
stage needs no workspace; accumulating a 200-document pool and evidence across stages usually does.

Upgrade to `sdk.workspace` only when a growing candidate pool, evidence ledger, or attempted-source
history must survive several stages, avoid replay, or recover after uncertain execution. Derive a
stable `runs/<research_id>/` namespace from the task, requirements, and source policy. Load needed
artifacts before capability calls; persist progress before `NEXT:` and print a bounded handoff.
Print any workspace paths needed for handoff; observations do not add workspace metadata, and
Python variables do not survive calls.

Public web URLs remain reusable; local IDs and workspace artifacts are session-bound. On explicit
`state_lost`, rebuild workspace artifacts and local-ID admission. For an unknown timeout or adapter
result, do not replay blindly: resume only work durable progress proves missing. After a final
capability failure, change the query, source, or candidate.

Return the final answer directly as your entire response, without wrapper tags or a preamble.
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
                "description": ("Run one Python research stage in the current OpenSAC session."),
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
            return render_error("invalid_program", "Expected non-empty Python code.")

        try:
            session_id = await self._ensure_session()
            payload = await self._session_client().exec_code(
                session_id,
                code,
                include_trace=False,
            )
            return self._render(payload)
        except httpx.TimeoutException:
            return render_error(
                "request_timeout",
                f"OpenSAC timed out after {DEFAULT_TIMEOUT_SECONDS:.0f}s.",
            )
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            return render_error(
                "request_failed",
                str(exc),
                exception_type=type(exc).__name__,
            )

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
