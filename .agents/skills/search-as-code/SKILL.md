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
- Rank and inspect text with `sdk.content.passages(...)`, `sdk.content.grep(...)`, and focused
  `sdk.content.read(...)` or `sdk.content.read_many(...)` windows.
- Treat optional `sdk.llm.extract_many(...)` as transformation, not as new evidence. Validate quotes
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
- Prefer `search -> passages or grep -> focused read` for long documents. Check `next_offset` or
  `scan_complete` before treating a bounded scan as exhaustive.
- Keep evidence source-scoped. For each requirement, record the inspected source, a bounded exact
  excerpt, whether it directly proves or only supports the claim, and any limitation. Verify a
  relation from one entailing excerpt or an explicit evidence-backed join; never concatenate
  documents and treat unrelated term matches as proof.

When extending prior work, filter repeated queries or sources in ordinary Python from the context
available to that program. Choose new inputs yourself.

## Compose pipeline programs

- Treat one program as one semantic checkpoint, not one SDK method. When outputs mechanically
  determine the next inputs, compose the useful chain in the same program, such as
  `search -> fuse -> passages or grep -> focused reads -> normalize`.
- Split into another `sac_run` only when the control model must make a new semantic choice, the next
  work needs a separate budget, or durable recovery is useful. Do not round-trip through stdout just
  to pass sources, offsets, or other values Python can derive directly.
- Normalize successful rows, typed failures, source provenance, and bounded evidence immediately
  after each capability. Derive later inputs and coverage from those structured rows rather than
  concatenated document text or printed observations.
- Persist compact artifacts needed by the next checkpoint. Avoid copying the same raw search hits or
  full documents into several ledger fields.
- End each checkpoint with a bounded decision surface built from the same normalized rows: useful
  candidate identifiers and sources, or per-requirement evidence excerpts, status, and failures.
  Counts alone are not enough when the next step needs semantic judgment, and should not force a
  later program whose only job is to reload state and print it.
- Make normalized row schemas total: represent a miss with an explicit status and empty fields rather
  than `None` where a mapping is expected. Capture excerpts and coordinates while handling the
  content result instead of rediscovering them later with formatting-sensitive regexes.

## Use state as a lightweight reusable data layer

Calls run in program mode, so variables do not survive. `sdk.state` is program-to-program memory;
there is no `sdk.workspace` API. Do not create workspace artifacts merely because research may use
more than one `sac_run`. Prefer one composed program, or carry a bounded normalized decision surface
through the visible observation, when that is sufficient.

Use `sdk.state` when later programs benefit from reusing search or content data. Prefer a small data
cache over a workflow state machine: a deduplicated candidate pool, inspected content windows, and
optional task/query/failure metadata are usually enough. Later programs must load and use those rows
to filter already searched queries or fetched sources; observations show artifact paths, not their
contents. Do not add per-stage logs, final ledgers, or duplicate raw reports unless the task actually
needs them.

Keep each cache cumulative: update the same pool and content artifacts by stable source or window
keys instead of creating `round2`, `stage3`, or similar files. A content-fetching program should also
print bounded target excerpts or explicit no-match, blocked, and failure summaries from the rows it
just stored. Use a state-only local program for a genuinely new question over cached data, not merely
to reveal content that the fetching program could have surfaced.

For an expensive call whose adapter outcome may be unknown, saving its attempted inputs before the
call can protect against blind replay. This is not required for every ordinary capability call. Read
[references/stateful-research.md](references/stateful-research.md) when choosing a reusable multi-call
data cache.

## Return observations and optional structured output

- Print compact progress, the bounded decision surface needed for the next judgment, and a `NEXT:`
  action. Do not print raw result lists, full passages, or the ledger; persist them. Keep stdout under
  about 4,000 characters by default.
- Agent completion is the final response to the user, not `sdk.output.submit(...)`.
- `submit` is optional. Use it for requested or downstream-consumed structured runtime output.
- Material claims, evidence, status, and citations in a submission must derive from capability
  results or loaded state. Runtime metrics alone do not make hand-authored prose program-derived.
  If the same substantive payload could be written before research ran, do not submit it.
- Do not submit to end a stage or report partial progress. A submission is a complete runtime
  artifact, not a progress marker.
- Do not run a separate finalization program merely to turn already visible evidence into prose or a
  redundant ledger. Answer directly unless recovery requires loading state or the caller needs a
  structured runtime result.

Before answering or submitting, require inspected evidence for each material constraint, derive its
citations, and preserve conflicts. Compute status from requirement coverage, not an expected answer;
use answered, partial, inconclusive, or externally blocked as appropriate.

## Handle failures and state loss

`sac_run` renders bounded item-failure warnings while preserving successes. Inspect typed failures
when branching and persist them when later completeness depends on them. Empty results without a
failure are successful reports. Never hard-code zero failures.

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
