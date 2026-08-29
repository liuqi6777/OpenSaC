---
name: search-as-code-repl
description: Run evidence-grounded OpenSAC research through sac_run when the host is explicitly configured for the experimental persistent_interpreter mode. Use only when invoked as $search-as-code-repl; use search-as-code for ordinary process-per-call sessions.
---

# Search as Code REPL

Invoke `sac_run(code)` with one complete Python cell. The MCP host binds the conversation and owns
the OpenSAC session; never create, display, or delete REST sessions or kernel identifiers.
`sac_run` is the outer adapter tool, not a Python API inside the sandbox. Put only the cell body in
the `code` argument; never call `sac_run` from inside that cell.

Import the SDK namespace and request-level error type when needed:

```python
from opensac_sdk import BrokerError, sdk
```

The first observation must report `execution_mode=persistent_interpreter`. If it reports another
mode or omits it, stop and report a configuration mismatch. Continue reusing the live interpreter
only while `interpreter_state=ready`; `not_started` has no reusable Python namespace yet.

Choose the strategy yourself. The persistent interpreter is a capability surface with optional live
memory, not a prescribed workflow. No fixed query count, capability sequence, cell split, variable
name, cleanup convention, or checkpoint schema is required.

## Use the capability surface and inspect evidence

- Search with `sdk.search(...)` or `sdk.search.many(...)`; use `sdk.search.fuse_rrf(...)` when
  fusion, domain policy, or diversity helps.
- Never print whole content documents.
- Pass source strings, not result records, to content. Public URLs are reusable; local IDs remain
  bound to this session.
- Use snippets for triage, not document claims. Inspect text for every material claim; treat mirrors,
  repeated records, and RRF agreement as one source family rather than corroboration.
- Treat search hits and fused results as candidates, not a fetch queue. Choose a relevant subset
  from metadata and unresolved requirements instead of fetching the whole result list.
- Once a candidate is promoted to body inspection, make `sdk.content.fetch(...)` or, for a selected
  batch, `sdk.content.fetch_many(...)` its first content call. Reuse successful returned documents
  for ordinary exact or regex matching, slicing, and cross-checks in local Python.
- Inspect fetched documents locally for relation checks and bounded evidence extraction. Do not use
  `sdk.content.grep(...)` or `sdk.content.read(...)` merely to relocate text already returned by
  fetch. Reserve them for a genuinely useful service-side window or cursor.
- Treat optional `sdk.llm.extract(...)` or aligned `sdk.llm.extract_many(...)` as transformation of
  supplied text, not new evidence. Validate quotes against its inputs.
- Keep evidence source-scoped. Record a bounded exact excerpt and limitation for each requirement;
  verify a relation from entailing text or an explicit evidence-backed join.

Read [references/sdk-contract.md](references/sdk-contract.md) for unfamiliar methods, limits,
failure types, or citations. Inspect one exact method's `__doc__` when necessary.

## Compose cells around semantic checkpoints

- Treat one cell as one semantic checkpoint, not one SDK call. When outputs mechanically determine
  the next inputs, compose the chain in that cell, such as
  `search -> select a relevant subset -> fetch -> local inspect -> normalize`.
- Start another cell when the control model must make a new semantic choice, a separate budget helps,
  or recovery/debugging is useful. Live variables may carry structured rows across that boundary;
  do not serialize or print them merely to pass sources and offsets.
- Use ordinary Python freely for deterministic orchestration: comprehensions, functions, data
  structures, regexes, sorting or grouping, source-scoped joins, deduplication, and validation.
  Choose the techniques that fit the task; this is not a required sequence or policy.
- Normalize aligned outcome statuses, content failures, provenance, bounded excerpts, and
  coordinates while handling each capability. Derive later inputs and coverage from those rows
  rather than concatenated text.
- End a checkpoint with the bounded candidates or evidence needed for the next judgment. Counts alone
  should not force another cell whose only purpose is to reveal already available rows.
- Reuse, replace, or delete live values as useful. Avoid accumulating parallel copies of raw reports
  or mirroring every live object to disk.

## Use live and durable state selectively

Python variables, functions, imports, and assignments completed before an ordinary exception survive
later cells while the interpreter remains ready. This live namespace is the default working memory.

Treat interpreter memory and filesystem persistence as independent mechanisms. Use `sdk.state` only
when a durable recovery cache or later program reuse saves meaningful external work, and only when
`sdk.session.capabilities()["mechanisms"]["persistence"]` is enabled. Prefer a small cumulative data
cache over per-cell logs, stage files, a final ledger, or a disk copy of the whole namespace.

Keep durable caches cumulative by stable source or window keys. A cell that fetches content should
also print bounded evidence, no-match, blocked, and failure summaries; do not require a state-only
cell merely to display what the fetching cell already had.

For recoverable external work, persist an input as `started` before the call when avoiding blind
replay matters. Immediately after the call, persist its `success` or `failure` before further
transformations. A surviving `started` means the outcome may be unknown. Keep the requested source
as an alias and the returned document source as the canonical content key.

After an uncertain adapter failure, inspect relevant globals and durable rows in a read-only cell.
Repeat an external operation only when that progress proves the work is missing.

After confirming persistence, use the
[optional durable fetch-cache pattern](references/patterns.md#optionally-cache-selected-fetches-across-calls)
when a later cell or recovery path will reuse the fetched text.

## Return observations and optional structured output

- Print compact progress and the bounded decision surface needed for the next judgment. A `NEXT:`
  line is useful when another semantic decision remains, but it is not a required cell protocol.
- Agent completion is the final response to the user, not `sdk.output.submit(...)`.
- `submit` is optional. Use it once only when the caller or downstream contract needs structured
  runtime output. Do not submit partial progress or print the same final payload first.
- Material claims, status, evidence, and citations in a submission must derive from inspected rows or
  trustworthy loaded state. Preserve conflicts and compute completeness from requirement coverage.
- Do not run a finalization cell merely to turn already visible evidence into prose. Answer directly
  unless recovery requires loading state or structured runtime output was requested.

Warnings, stdout, stderr, and submitted output share one observation budget; keep each bounded.

## Handle interpreter and adapter failures

For `search.many`, branch on `status == "success"` and read failed rows from `outcome.error`; status
is only `"success"` or `"failure"`. For `content.grep`, other statuses are human-readable and must
not be parsed. Empty hits or matches with success status are successful results. A caught
`BrokerError` must leave the checkpoint incomplete with a bounded error or next action. Let host
policy own retries.

If an observation reports `interpreter_state=lost` or `state_lost`, the submitted cell is never
replayed and the next invocation starts clean. Restore a trustworthy checkpoint if one exists,
re-admit local sources, and recompute only work not supported by saved evidence. Public URLs remain
reusable.

## Load examples only when useful

- Read [references/patterns.md](references/patterns.md) for composition, optional structured
  extraction, and durable-cache patterns.

Treat every example as a starting point rather than a required pipeline. Adapt or ignore its query
count, ordering, cell boundaries, variable names, source policy, and checkpoint schema.
