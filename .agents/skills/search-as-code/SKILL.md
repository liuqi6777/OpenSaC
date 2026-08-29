---
name: search-as-code
description: Run evidence-grounded research with OpenSAC through the sac_run MCP tool. Use when sac_run is available for programmable search, source discovery, document inspection, fact checking, structured extraction, stateful multi-call research, or URL-cited structured results.
---

# Search as Code

Run one complete Python program through the model-visible MCP tool `sac_run(code)`. Import
`BrokerError` and `sdk` from `opensac_sdk` when needed.

Choose the strategy yourself; this skill teaches how to encode it as OpenSAC code. No fixed query
count, capability sequence, stage split, or workspace schema is required.

## Use the capability surface

- Search with `sdk.search(...)` or `sdk.search.many(...)`; use `sdk.search.fuse_rrf(...)` when
  fusion, domain policy, or diversity helps.
- Never print whole content documents.
- Treat optional `sdk.llm.extract(...)` as transformation, not as new evidence. Validate quotes
  against its inputs.
- Read usage or deployment capabilities with `sdk.session` when needed, and use `sdk.state` for
  artifacts. Use `sdk.output.submit(...)` only for a complete runtime result needed through
  `ExecResult.output`.

Read [references/sdk-contract.md](references/sdk-contract.md) for unfamiliar methods, failures,
limits, or citations. Inspect one method's `__doc__` when necessary.

## Ground claims in inspected evidence

- Pass source strings, never result records, to content. Public URLs are directly readable; local IDs
  require search admission.
- Use snippets for triage, not document claims. Inspect text for every material claim; metadata alone
  supports only a discovery list.
- Treat mirrors, repeated catalog records, and RRF agreement as one source family, not independent
  corroboration.
- Generate optional citation labels from inspected evidence; submission does not validate them.
- Treat search hits and fused results as candidates, not a fetch queue. From their titles, snippets,
  provenance, source quality, and the unresolved requirements, choose the smallest source-diverse
  set likely to add evidence. Do not fetch the whole result list; expand with another relevant batch
  only when inspected evidence leaves a gap.
- Once a candidate is promoted to body inspection, make `sdk.content.fetch(...)` its first content
  call. Reuse the returned text for all ordinary exact or regex matching, slicing, and cross-checks
  in local Python; persist one copy only when later programs will reuse it.
- `sdk.content.passages(...)` adds semantic passage ranking that ordinary local matching does not.
  Use it after fetch when semantic localization across long or multiple selected documents is useful,
  alone or after local checks. For each semantic question, pass only the fetched sources plausibly
  relevant to it; avoid running every question over every selected source. Do not use
  `sdk.content.grep(...)` or `sdk.content.read(...)` merely to relocate text already present in a
  fetched document—compute matches, coordinates, and excerpts locally. Reserve them for a genuinely
  useful service-side window or cursor. Never pass an unfetched source to a content method.
- Keep evidence source-scoped. For each requirement, record the inspected source, a bounded exact
  excerpt, whether it directly proves or only supports the claim, and any limitation. Verify a
  relation from one entailing excerpt or an explicit evidence-backed join; never concatenate
  documents and treat unrelated term matches as proof.

When extending prior work, filter repeated queries or sources in ordinary Python from the context
available to that program. Choose new inputs yourself.

## Compose pipeline programs

- Treat one program as one semantic checkpoint, not one SDK method. When outputs mechanically
  determine the next inputs, compose the useful chain in the same program, such as
  `search -> select a relevant subset -> fetch -> local inspect and/or passages -> normalize`.
- Split into another `sac_run` only when the control model must make a new semantic choice, the next
  work needs a separate budget, or durable recovery is useful. Do not round-trip through stdout just
  to pass sources, offsets, or other values Python can derive directly.
- Normalize statuses, passage failures, provenance, and bounded evidence immediately. Derive later
  inputs from source-scoped rows, not concatenated text or printed observations.
- Persist compact artifacts needed by the next checkpoint. Avoid copying the same raw search hits or
  full documents into several ledger fields.
- End each checkpoint with the candidates or per-requirement excerpts, status, and failures needed
  for the next judgment; counts alone are insufficient.
- Make normalized row schemas total: represent a miss with an explicit status and empty fields rather
  than `None` where a mapping is expected. Capture excerpts and coordinates while handling the
  content result instead of rediscovering them later with formatting-sensitive regexes.

## Use state as a lightweight reusable data layer

Variables do not survive program mode. `sdk.state` is program-to-program memory; there is no
`sdk.workspace` API. Prefer one composed program or a bounded visible decision surface when enough.

Use `sdk.state` only when later programs benefit from reusing data. Prefer a small data cache over a
workflow state machine. Load its rows to filter prior queries and fetched sources; observations show
artifact paths, not their contents. Avoid duplicate raw reports and per-stage ledgers.

Keep each cache cumulative: update the same pool and content artifacts by stable keys. Print bounded
target excerpts or explicit no-match/failure summaries when storing content; do not add a program
merely to reload and print it.

Read [references/stateful-research.md](references/stateful-research.md) when a reusable multi-call
cache is justified.

## Return observations and optional structured output

- Print compact progress, the bounded decision surface needed for the next judgment, and a `NEXT:`
  action. Do not print raw result lists, full passages, or the ledger; persist them. Keep stdout under
  about 4,000 characters across the whole program, not independently per query or loop. Prefer a few
  nonredundant excerpts that together expose the remaining decision.
- Agent completion is the final response to the user, not `sdk.output.submit(...)`.
- `submit` is optional. Use it for requested or downstream-consumed structured runtime output.
- Material claims, evidence, status, and citations in a submission must derive from capability
  results or loaded state. Runtime metrics alone do not make hand-authored prose program-derived.
  If the same substantive payload could be written before research ran, do not submit it.
- Do not submit partial progress or run a separate program merely to reformat visible evidence.

Before answering or submitting, require inspected evidence for each material constraint, derive its
citations, and preserve conflicts. Compute status from requirement coverage, not an expected answer;
use answered, partial, inconclusive, or externally blocked as appropriate.

## Handle failures and state loss

`sac_run` renders bounded structured item-failure warnings while preserving successes. For
`search.many`, branch on `status == "success"` and read failed rows from `outcome.error`; status is
only `"success"` or `"failure"`. For `content.grep`, other statuses are human-readable and must not
be parsed. Passage failures remain structured records. Empty hits or matches with success status
are successful results. Never hard-code zero failures.

An intermediate caught `BrokerError` must leave the stage incomplete with `ERROR:` or `NEXT:`. Let
host policy own retries; do not retry blindly.

Public web URLs remain reusable across sessions; local IDs remain session-bound. If `sac_run`
returns `state_lost`, the submitted program was not replayed; rebuild state and local-ID admission.
Adapter failures occur outside the sandbox, so their execution outcome may be unknown. Inspect state
and usage instead of replaying the same program blindly; report OpenSAC unavailable after repeated
inspection failure.

## Load examples only when useful

- Read [references/patterns.md](references/patterns.md) for the default stateless composition patterns.
- Read [references/stateful-research.md](references/stateful-research.md) only for a chosen
  workspace-backed multi-call design.
- Read [references/python-recipes.md](references/python-recipes.md) for bounded query generation,
  aggregation, or extraction-driven actions.
- Read [references/advanced.md](references/advanced.md) only when core workflows are insufficient.

Treat every example as a starting point rather than a required pipeline. Adapt or ignore its query
count, ordering, boundaries, source policy, and artifact schema.
