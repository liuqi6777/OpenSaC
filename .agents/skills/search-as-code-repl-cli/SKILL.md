---
name: search-as-code-repl-cli
description: Run evidence-grounded OpenSAC research through agent-run when the CLI is explicitly configured for the experimental persistent_interpreter mode. Use only when invoked as $search-as-code-repl-cli; use search-as-code-cli for ordinary process-per-call sessions.
---

# Search as Code REPL CLI

Pipe one complete Python cell to `opensac agent-run`:

```bash
opensac agent-run <<'OPENSAC_PY'
from opensac_sdk import BrokerError, sdk
print(sdk.session.usage())
OPENSAC_PY
```

Use a quoted heredoc and pass code only on stdin. The host binds the conversation; never create,
display, or delete REST sessions or kernel identifiers. If the command reports `context_*` or
`configuration_error`, stop. `opensac agent-run` is the outer adapter command. Keep the heredoc body
as plain Python; never place `sac_run(...)` or another `agent-run` command inside it. If `opensac` is
not on `PATH` and the current workspace is an OpenSaC source checkout, launch the same stdin cell with
`uv run opensac agent-run`. If neither launcher exists, stop and report setup failure.

The first observation must report `execution_mode=persistent_interpreter`. If it reports another
mode or omits it, stop and report a configuration mismatch. Continue reusing the live interpreter
only while `interpreter_state=ready`; `not_started` has no reusable Python namespace yet.

Choose the strategy yourself. The persistent interpreter is a capability surface with optional live
memory, not a prescribed workflow. No fixed query count, capability sequence, cell split, variable
name, cleanup convention, or checkpoint schema is required.

## Use the capability surface and inspect evidence

- Read the rendered `[sac_run]` observation, including sandbox exit code, stderr, warnings, and
  interpreter state; shell status alone is insufficient.
- Search with `sdk.search(...)` or `sdk.search.many(...)`; use `sdk.search.fuse_rrf(...)` when
  fusion, domain policy, or diversity helps.
- Rank and inspect text with `sdk.content.passages(...)`, `sdk.content.grep(...)`, and focused
  `sdk.content.read(...)` windows. Loop in Python when several independent reads are needed.
- Pass source strings, not result records, to content. Public URLs are reusable; local IDs remain
  bound to this session.
- Use snippets for triage, not document claims. Inspect text for every material claim; treat mirrors,
  repeated records, and RRF agreement as one source family rather than corroboration.
- Treat optional `sdk.llm.extract(...)` as transformation of supplied text, not new evidence.
  Validate quotes against its inputs.
- Keep evidence source-scoped. Record a bounded exact excerpt and limitation for each requirement;
  verify a relation from entailing text or an explicit evidence-backed join.

Read [references/sdk-contract.md](references/sdk-contract.md) for unfamiliar methods, limits,
failure types, or citations. Inspect one exact method's `__doc__` when necessary.

## Compose cells around semantic checkpoints

- Treat one cell as one semantic checkpoint, not one SDK call. When outputs mechanically determine
  the next inputs, compose the chain in that cell, such as
  `search -> fuse -> passages or grep -> focused reads -> normalize`.
- Start another cell when the control model must make a new semantic choice, a separate budget helps,
  or recovery/debugging is useful. Live variables may carry structured rows across that boundary;
  do not serialize or print them merely to pass sources and offsets.
- Normalize aligned outcome statuses, structured passage failures, provenance, bounded excerpts,
  and coordinates while handling each capability. Derive later inputs and coverage from those rows
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
`sdk.session.capabilities()["mechanisms"]["persistence"]` is enabled. A small cumulative cache of
optional metadata, a deduplicated candidate pool, and inspected content windows is usually enough.
Do not create per-cell logs, stage files, a final ledger, or a disk copy of the whole namespace.

Keep durable caches cumulative by stable source or window keys. A cell that fetches content should
also print bounded evidence, no-match, blocked, and failure summaries; do not require a state-only
cell merely to display what the fetching cell already had.

After an uncertain adapter failure, inspect relevant globals and `sdk.session.usage()` in a read-only
cell before repeating an external operation. Saving attempted inputs before an expensive call is
useful only when avoiding blind replay justifies it.

Read [references/stateful-research.md](references/stateful-research.md) for optional cumulative cache,
recovery, and namespace-inspection examples.

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

For `search.many` and `content.grep`, branch only on `status == "success"`; any other status is
human-readable and must not be parsed. Passage failures remain structured records. Empty hits or
matches with success status are successful results. A caught `BrokerError` must leave the checkpoint
incomplete with a bounded error or next action. Let host policy own retries.

If an observation reports `interpreter_state=lost` or `state_lost`, the submitted cell is never
replayed and the next invocation starts clean. Restore a trustworthy checkpoint if one exists,
re-admit local sources, and recompute only work not supported by saved evidence. Public URLs remain
reusable.

Adapter `HTTP 401` or `HTTP 403` means credential setup failed; report it without exposing
credentials. Other adapter failures have an unknown execution outcome, so inspect before resuming.

## Load examples only when useful

- Read [references/patterns.md](references/patterns.md) for composed retrieval and optional semantic
  checkpoint examples.
- Read [references/python-recipes.md](references/python-recipes.md) for optional live Python fragments.
- Read [references/advanced.md](references/advanced.md) only when core workflows are insufficient.

Treat every example as a starting point rather than a required pipeline. Adapt or ignore its query
count, ordering, cell boundaries, variable names, source policy, and checkpoint schema.
