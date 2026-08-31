---
name: search-as-code
description: Run evidence-grounded research with OpenSAC through the sac_run MCP tool. Use when sac_run is available for programmable search, source discovery, document inspection, fact checking, structured extraction, persistent multi-call research, or URL-cited structured results.
---

# Search as Code

Run one complete Python program through the model-visible MCP tool `sac_run(code)`. Import
`sdk` from `opensac_sdk` when needed.

Choose the research strategy yourself. This skill teaches how to encode that strategy as OpenSAC
code; it does not prescribe query counts, source choices, call stages, stopping rules, or one
workspace schema.

## Core orchestration contract

- Treat one program as one semantic checkpoint. Compose mechanically linked capability calls and
  local transformations in that program; split only when the agent must make a new semantic choice,
  a separate output budget is useful, or durable recovery matters.
- Search results are candidate data. Pass source strings, not result records, to content methods.
  Fetch only the selected sources, inspect returned bodies locally, and keep each evidence excerpt
  beside its source and material requirement.
- If another program may reuse a successful body, persist it immediately after fetch and before
  parsing. Later load the exact selected artifact instead of fetching again merely to try another
  parser or render the same evidence.
- Local parsing is local computation. When body shape is uncertain, try reasonable parser variants
  in one program and validate the task invariant—such as cardinality, unique keys, field shape, or
  source alignment—before a downstream capability call.
- Represent cumulative state with stable material keys. Start unresolved fields as unknown, keep
  independently testable fields and relations separate, and carry failures and source conflicts.
  For batches, align results to inputs and derive later inputs from validated rows rather than
  retyping an anticipated list.
- Preserve zero, one, or many records when a unit can yield multiple results. Do not collapse a
  record set to one convenient match; derive coverage from the normalized rows that will support the
  answer.
- Across calls, use `sdk.workspace` as a compact reusable data layer. The final response must follow
  the current full state, not a remembered answer or a subset printed in an earlier observation.

These are dataflow invariants, not a required pipeline. Completion is an agent-level evidence
judgment, not a code token. stdout is a bounded audit projection; code does not need to print a
completion label or every final row.

## Capability and evidence basics

- Search with `sdk.search(...)` or `sdk.search.many(...)`; fuse aligned batches with
  `sdk.search.fuse_rrf(queries, results, ...)` when useful.
- Material claims require inspected page text. Snippets help select sources but are not claim
  evidence. Mirrors count as one source family.
- Make `sdk.content.fetch(...)` or `sdk.content.fetch_many(...)` the first content call for a
  selected source. Do not use `sdk.content.grep(...)` or `sdk.content.read(...)` merely to
  relocate text already present in a fetched body, and never pass an unfetched source to content.
- Use deterministic Python for parsing, joins, deduplication, validation, and coverage. Keep claims
  atomic enough to validate independently: nearby terms are candidates for a relation, not proof of
  it. Give role- or time-sensitive relations explicit scope.
- Read `sdk.capabilities()` only when a deployment mechanism or limit changes the program.

Read [the SDK contract](references/sdk-contract.md) for unfamiliar signatures, result fields,
failures, limits, and lifecycle semantics.

## Repeated and multi-call work

Batch repeated units instead of using one `sac_run` per row. Validate upstream keys and membership
before fan-out, map every available or missing result back to its input, and persist cumulative rows only when
a later semantic checkpoint will reuse them.

For closed sets or one-to-many enumerations, read
[repeated units and record sets](references/repeated-units.md). For uncertain local parsers,
selected-artifact binding, or exact relation/conflict state, read
[optional orchestration helpers](references/orchestration.md). These helpers are adaptable checks,
not mandatory schemas.

For recoverable external calls, persist a `started` marker only when replay ambiguity matters, then
persist item availability or unresolved inputs before later transformation. Reconcile surviving
state from durable data rather than blindly replaying the call.

## Return bounded observations

Collect rows first and print once. Keep one global soft budget of about 4,000 characters across
normal rows, failures, and total/shown/omitted counts. Every shown evidence row keeps its source.
Do not print raw result lists, complete documents, or workspace ledgers.

The program output need not say whether research should continue. Once the full in-memory or
persisted state supports the request, answer the user directly; if material fields or enumeration
scope remain unresolved, report a partial or inconclusive result. No finalization-only call is
needed just to announce completion.

## Handle failures and state loss

Broker-backed single-item methods return a result or `None`; fan-out methods return an input-aligned
list with `None` in failed positions. Check `is None`, never truthiness, because empty lists,
strings, or objects can be successful results. Preserve the original inputs and use
`zip(inputs, results, strict=True)` when failed-item identity matters. OpenSAC renders bounded
external-failure warnings automatically, so do not add `try/except` or print failures merely to
expose them. Persist unresolved inputs when later dataflow must remember them, and do not blindly
retry them.

Public web URLs remain reusable; local IDs are session-bound. On `state_lost`, the program was not
replayed, so rebuild workspace artifacts and local-ID admission. Adapter failures occur outside the
sandbox and may leave the execution result unknown; inspect durable state before replaying.

Treat every example as a starting point, not a required pipeline.
