---
name: search-as-code-cli
description: Run evidence-grounded OpenSAC Python research through the local agent-run CLI. Use in shell-capable environments for multi-query search, document inspection, fact checking, extraction, persistent workspace use, or URL-cited results without MCP.
---

# Search as Code CLI

Pipe one complete Python research program to `opensac agent-run`:

```bash
opensac agent-run <<'OPENSAC_PY'
from opensac_sdk import BrokerError, sdk

# Replace this probe with a complete program from references/patterns.md.
print(sdk.capabilities())
OPENSAC_PY
```

Use a quoted heredoc; send the program only on stdin, never as a shell argument. Never expose or
override its identity, manage REST
sessions, or call OpenSAC endpoints directly. If `opensac` is not on `PATH` and the current workspace
is an OpenSaC source checkout, use `uv run opensac agent-run` with the same stdin program. If neither
launcher is available, or the command reports `context_*` or `configuration_error`, stop and report
setup failure.

Choose the strategy yourself; this skill teaches how to encode it as OpenSAC code. No fixed query
count, capability sequence, stage split, or workspace schema is required.

## Use the capability surface

- Search with `sdk.search(...)` or `sdk.search.many(...)`; use `sdk.search.fuse_rrf(...)` when
  fusion, domain policy, or diversity helps.
- Never print whole content documents.
- Treat optional `sdk.llm.extract(...)` or aligned `sdk.llm.extract_many(...)` as transformation,
  not as new evidence. Validate quotes against their inputs.
- Read deployment capabilities with `sdk.capabilities()` when needed, and use `sdk.workspace` for
  artifacts. Return bounded results with `print(...)`.

Read [references/sdk-contract.md](references/sdk-contract.md) for unfamiliar methods, failures,
limits, or lifecycle semantics. Inspect one method's `__doc__` when necessary.

## Ground claims in inspected evidence

- Read the rendered `[sac_run]` observation, including sandbox exit code, stdout, stderr, and
  warnings. Shell status alone is insufficient.
- Pass source strings, never result records, to content. Public URLs are directly readable; local IDs
  require search admission.
- Use snippets for triage, not document claims. Inspect text for every material claim; metadata alone
  supports only a discovery list.
- Treat mirrors, repeated catalog records, and RRF agreement as one source family, not independent
  corroboration.
- Carry exact source URLs or local IDs beside the printed evidence they support.
- Treat search hits and fused results as candidates, not a fetch queue. From their titles, snippets,
  provenance, source quality, and the unresolved requirements, choose the smallest source-diverse
  set likely to add evidence. Do not fetch the whole result list; expand with another relevant batch
  only when inspected evidence leaves a gap.
- Once a candidate is promoted to body inspection, make `sdk.content.fetch(...)` or, for a selected
  batch, `sdk.content.fetch_many(...)` its first content call. Reuse successful returned documents
  for all ordinary exact or regex matching, slicing, and cross-checks in local Python; persist one
  copy only when later programs will reuse it.
- Inspect successful fetched documents with local Python for exact matching, regexes, slicing,
  relation checks, and bounded evidence extraction. Do not use `sdk.content.grep(...)` or
  `sdk.content.read(...)` merely to relocate text already present in a fetched document. Reserve them
  for a genuinely useful service-side window or cursor. Never pass an unfetched source to a content
  method.
- Keep evidence source-scoped. For each requirement, record the inspected source, a bounded exact
  excerpt, whether it directly proves or only supports the claim, and any limitation. Verify a
  relation from one entailing excerpt or an explicit evidence-backed join; never concatenate
  documents and treat unrelated term matches as proof.

When extending prior work, filter repeated queries or sources in ordinary Python from the context
available to that program. Choose new inputs yourself.

## Compose pipeline programs

- Treat one program as one semantic checkpoint, not one SDK method. When outputs mechanically
  determine the next inputs, compose the useful chain in the same program, such as
  `search -> select a relevant subset -> fetch -> local inspect -> normalize`.
- Split into another `agent-run` only when the control model must make a new semantic choice, the
  next work needs a separate budget, or durable recovery is useful. Do not round-trip through stdout
  just to pass sources, offsets, or other values Python can derive directly.
- Use ordinary Python freely for deterministic orchestration: comprehensions, functions, data
  structures, regexes, sorting or grouping, source-scoped joins, deduplication, and validation.
  Choose the techniques that fit the task; this is not a required sequence or policy.
- Normalize statuses, content failures, provenance, and bounded evidence immediately. Derive later
  inputs from source-scoped rows, not concatenated text or printed observations.
- Persist compact artifacts needed by the next checkpoint. Avoid copying the same raw search hits or
  full documents into several ledger fields.
- End each checkpoint with the candidates or per-requirement excerpts, status, and failures needed
  for the next judgment; counts alone are insufficient.
- Make normalized row schemas total: represent a miss with an explicit status and empty fields rather
  than `None` where a mapping is expected. Capture excerpts and coordinates while handling the
  content result instead of rediscovering them later with formatting-sensitive regexes.

## Use the workspace as a lightweight reusable data layer

Variables do not survive program mode. `sdk.workspace` persists structured artifacts between
programs. Prefer one composed program or a bounded visible decision surface when enough.

Use `sdk.workspace` only when later programs benefit from reusing data. Prefer a small data cache
over a workflow state machine. Load its rows to filter prior queries and fetched sources;
observations show artifact paths, not their contents. Avoid duplicate raw reports and per-stage
ledgers.

Keep each cache cumulative and update its rows by stable keys. Print bounded target excerpts or
explicit no-match/failure summaries when storing content; do not add a program merely to reload and
print it.

For recoverable multi-call work, persist an operation as `started` before an expensive external call
when avoiding blind replay matters. Immediately after the call, persist each input as `success` or
`failure` before running further transformations. A surviving `started` status means the outcome may
be unknown and must be reconciled from durable workspace data, not blindly replayed. Use the
returned document source as the stable content key and retain requested URL variants only as aliases.

For executable recovery ordering, read the
[optional durable fetch-cache pattern](references/patterns.md#optionally-cache-selected-fetches-across-calls).

## Return bounded observations

- Print compact progress, the bounded decision surface needed for the next judgment, and a `NEXT:`
  action. Do not print raw result lists, full documents, or the ledger; persist them. Include exact
  source strings beside evidence. Warnings, stdout, and stderr share one observation budget. Bound
  output across the whole program, not independently per query or loop; prefer a few nonredundant
  excerpts.
- Agent completion is the final response to the user. Once printed evidence covers the request and no
  unresolved `NEXT:` remains, answer directly without a separate finalization program.
- Material claims, evidence, status, and source strings in stdout must derive from capability results
  or loaded workspace data. Runtime metrics alone do not make hand-authored prose program-derived.

Before answering, require inspected evidence for each material constraint, retain its source, and
preserve conflicts. Compute status from requirement coverage, not an expected answer;
use answered, partial, inconclusive, or externally blocked as appropriate.

## Handle failures and state loss

`[sac_run]` renders bounded structured item-failure warnings while preserving successes. For
`search.many`, branch on `status == "success"` and read failed rows from `outcome.error`; status is
only `"success"` or `"failure"`. For `content.grep`, other statuses are human-readable and must not
be parsed. Empty hits or matches with success status are successful results. Never hard-code zero
failures.

An intermediate caught `BrokerError` must leave the program incomplete with `ERROR:` or `NEXT:`.
Let host policy own retries; do not retry blindly.

Public web URLs remain reusable across sessions; local IDs remain session-bound. If the observation
reports `state_lost`, the program was not replayed; rebuild workspace artifacts and local-ID
admission.
Adapter failures occur outside the sandbox, so execution outcome may be unknown. Inspect durable
workspace data instead of replaying the same program blindly; if it cannot prove the work is
missing, report the outcome as unknown.

An adapter `HTTP 401` or `HTTP 403` means host credential setup failed. Stop and report it without
printing or embedding any credential.

## Load examples only when useful

- Read [references/patterns.md](references/patterns.md) for composition, optional structured
  extraction, and durable-cache patterns.

Treat every example as a starting point rather than a required pipeline. Adapt or ignore its query
count, ordering, boundaries, source policy, and artifact schema.
