---
name: search-as-code
description: Run evidence-grounded research with OpenSAC through the sac_run MCP tool. Use when sac_run is available and Codex needs programmable web or local search, source discovery, document inspection, fact checking, structured extraction, stateful multi-call research, or URL-cited structured results.
---

# Search as Code

Invoke the model-visible MCP tool `sac_run(code)` with one complete Python program. Never create,
resume, or delete REST sessions; the MCP host binds the current agent conversation through request
metadata. Import the SDK namespace and request-level error type when needed:

```python
from opensac_sdk import BrokerError, sdk
```

Treat OpenSAC as a capability surface, not a prescribed research workflow. Choose the research
strategy from the task and observations. No fixed query count, capability sequence, stage split, or
workspace schema is required. Combine, repeat, skip, or separate capabilities as useful.

Each call runs in program mode, so Python variables do not survive it. Use ordinary Python for query
construction, filtering, joins, validation, and aggregation. Persist JSON or JSONL through
`sdk.state` only when later programs need durable memory.

## Use the SDK surface

- Use `sdk.search(...)` for one query and `sdk.search.many(...)` for several queries. Use
  `sdk.search.fuse_rrf(...)` when local fusion, domain policy, or result diversity is useful.
- Use `sdk.content.passages(...)` to rank relevant passages across sources, `sdk.content.grep(...)`
  for literal or regex matching, and `sdk.content.read(...)` or `sdk.content.read_many(...)` for
  deliberate context windows.
- Use optional `sdk.llm.extract_many(...)` for schema-constrained semantic extraction. Treat its
  output as a transformation of supplied text, not as new evidence. Validate returned quotes against
  the input text.
- Use `sdk.session.usage()` and `sdk.session.capabilities()` for the active deployment, `sdk.state`
  for workspace artifacts, and `sdk.output.submit(...)` for the final structured result.

Read [references/sdk-contract.md](references/sdk-contract.md) before using unfamiliar methods,
fields, failures, limits, or citation behavior. To inspect the installed version, print one exact
method's `__doc__`; do not dump all runtime docs into the shared observation budget.

## Keep evidence boundaries intact

- Sources are URL or local-ID strings. Web content accepts bounded public HTTP(S) URLs directly;
  local IDs still require search admission. Pass strings, never result records, to content.
- Use search snippets to triage sources, not to support claims about document content. Search
  metadata is sufficient only when the requested result is a discovery list.
- Inspect the document text used for each material claim. Choose source quality and corroboration
  rules for the task; RRF agreement across queries is not independent-source corroboration.
- Treat output citations as optional, unverified source labels. Pass the inspected URL or local-ID
  strings; submission does not validate them against the answer.

## Return observations and results

- Use stdout for intermediate observations. Keep it bounded because warnings, stdout, stderr, and
  submitted output share one observation budget. Print reusable sources and enough text or metadata
  for the next judgment.
- Call `sdk.output.submit(...)` once when the research result is complete. Submit compact evidence
  and source labels without printing the same final payload first. After `submitted output` appears,
  stop calling `sac_run` and answer.

A final research result must use `submit`; stdout is not a substitute.

## Handle state and failures

Use the session workspace as program-to-program memory through `sdk.state`; no `sdk.workspace` API
exists. Choose a namespace and artifact shape only when persistence helps. Later programs must list
and read their artifacts; observations show artifact paths, not their contents.

`sac_run` renders bounded external-failure warnings before stdout while preserving successful rows.
Inspect typed item failures when code must branch on them. Empty hits or zero matches without a
failure are successful results, not transport failures. Catch `BrokerError` when a request failure
needs a different path; do not add blind retries around host-managed capability calls.

Public web URLs remain reusable across calls and sessions; local IDs remain session-bound. If
`sac_run` returns `state_lost`, the submitted program was not replayed; rebuild workspace state and
local-source admission before deciding what work remains.

Adapter failures and tool timeouts occur outside the sandbox, so their execution outcome may be
unknown. Do not replay the same program blindly. Inspect relevant workspace state and session usage
before choosing whether and how to continue. If inspection repeatedly fails, report OpenSAC as
unavailable.

## Load examples only when useful

- Read [references/advanced.md](references/advanced.md) only when a core workflow is insufficient.
- Read [references/patterns.md](references/patterns.md) for small, adaptable program examples.
- Read [references/python-recipes.md](references/python-recipes.md) when query variants should be
  generated systematically, search results filtered locally, coverage aggregated, or extraction
  output should inform another capability call.
- Read [references/stateful-research.md](references/stateful-research.md) for one adaptable example
  of workspace-backed multi-call research.

Treat every example as a starting point rather than a required pipeline. Adapt or ignore its query
count, ordering, boundaries, source policy, and artifact schema.
