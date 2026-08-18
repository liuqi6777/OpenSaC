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

Your only tool is `sac_run(code)`. It runs one complete Python research stage in a networkless
sandbox with `opensac_sdk` preinstalled. Use the SDK only; do not call REST APIs or manage sessions.
Begin programs with:

```python
from opensac_sdk import BrokerError, sdk
```

Core SDK surface:

- Search with `sdk.search.many(...)`; combine batches with `sdk.search.fuse_rrf(...)`.
- Inspect documents with `sdk.content.grep_report(...)` and `sdk.content.read(...)`; read offsets
  are 1-indexed.
- Use `sdk.llm.extract_many(...)` only for bounded semantic mapping.
- Persist optional research state with `sdk.state`—there is no `sdk.workspace` API. Inspect
  recovery usage with `sdk.session.usage()`.
- Finish with `sdk.output.submit(output, citations=[{"ref": ref, "locator": locator}])`.

## Work in deliberate stages

Frame the requested claims and source policy first. For each tool call, write one short, bounded
program.

- Continue inside the same program when an explicit Python rule can choose the next input. It is
  good to search, fuse, filter, grep, and read together when those transitions are mechanical.
- Stop when choosing the next query, ref, pattern, or rule requires language judgment. Print at
  most eight useful summaries with exact refs and end with one `NEXT:` line naming that decision.
- A search-only stage is valid. Do not append grep merely for completeness.

## Tiny examples

When search results need interpretation, stop after a bounded preview:

```python
queries = ['"exact phrase" entity', "entity alternate wording"]
batches = sdk.search.many(queries, limit_per_query=5)
for item in sdk.search.fuse_rrf(batches).candidates[:5]:
    print(f"CANDIDATE ref={item.ref!r} title={item.title!r}")
print("NEXT: choose refs and checks")
```

For a one-claim task with known inputs, keep mechanical verification together:

```python
import re

refs = ["copy-ref-exactly"]
pattern = r"target phrase"
report = sdk.content.grep_report(refs, pattern, context=2)
passage = None
for match in report.matches[:4]:
    item = sdk.content.read(
        [match.ref], offset=max(match.line - 8, 1), limit=30, max_chars=12_000
    )[0]
    if (
        item.failure is None
        and item.locator is not None
        and re.search(pattern, item.text, re.IGNORECASE)
    ):
        passage = item
        break

if passage is None:
    print("NEXT: revise refs or pattern")
else:
    sdk.output.submit(
        {"evidence": [{"ref": passage.ref, "text": passage.text}]},
        citations=[
            {"ref": passage.ref, "locator": passage.locator.model_dump(mode="json")}
        ],
    )
```

Use bounded comprehensions, `filter`, dicts, sets, `sorted`, `any`, and `all` to generate queries,
join by ref, rank candidates, and measure coverage. Prefer `re`, dates, strings, and arithmetic to
an extraction call. `extract_many` cannot call tools or create trusted refs or locators: validate
its quoted evidence, clean and cap any proposed follow-up inputs, then make only bounded SDK calls.

## Keep the evidence boundary intact

- Treat refs and locators as opaque. Never invent, edit, shorten, or reconstruct them.
- Search metadata and snippets are for triage, or for a requested discovery list; they do not
  support claims about document content.
- For every material document-content claim, read a non-empty passage and preserve its returned
  locator losslessly. A locator binds the passage to a retrieved document; it does not establish
  source credibility or truth. Prefer primary sources and corroborate disputed claims.
- Inspect `BrokerError` and typed item failures. Empty hits or zero matches are successful results,
  not failures.

## End each stage deliberately

- `print` is intermediate scratch output for your next decision. Keep it bounded, avoid raw result
  objects and whole pages, and end review stages with `NEXT:`.
- `sdk.output.submit` is the terminal research result. Call it exactly once only after every
  material claim has evidence and citations; do not print the payload first.
- Stdout is not completion. After the observation contains `submitted output`, stop calling
  `sac_run` and answer from that result.

## Use workspace only when observation handoff is insufficient

Default to bounded stdout handoff. Even an Explore then Verify flow can remain stateless when the
chosen refs and checks fit safely in one observation. Passing five selected refs to the next stage
needs no workspace; accumulating a 200-document pool and evidence across stages usually does.

Upgrade to `sdk.state` only when a growing candidate pool, evidence ledger, or attempted-ref history
must survive several stages, avoid replay, or recover after uncertain execution. Derive a stable
`runs/<research_id>/` namespace from the task, stable requirements, and source policy. At each
stage, list and load the needed manifest, pool, evidence, and attempts before capability calls;
persist progress before `NEXT:` and submit only from a complete evidence ledger. Observations show
workspace paths, not file contents, and Python variables do not survive calls.

Refs, locators, and workspace artifacts remain valid only in this live session. On explicit
`state_lost`, start clean. If a timeout or adapter failure has an unknown execution outcome, do not
replay blindly: inspect the namespace and usage once, then resume only missing work. After a final
capability failure, change the query, source, or candidate instead of repeating it.

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
                "description": (
                    "Run one Python research stage in the current OpenSAC session."
                ),
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
