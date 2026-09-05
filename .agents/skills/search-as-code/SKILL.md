---
name: search-as-code
description: Run evidence-grounded research with OpenSAC through the sac_run MCP tool. Use when sac_run is available for programmable search, source discovery, document inspection, fact checking, structured extraction, persistent multi-call research, or URL-cited structured results.
---

# Search as Code

Run one complete Python program through the model-visible MCP tool `sac_run(code)`. Import
`sdk` from `opensac_sdk` when needed.

Choose queries, sources, stages, stopping criteria, and artifact layouts to fit the user's task.
This skill provides SDK contracts and dataflow checks for implementing that research strategy.

## Core orchestration contract

- Treat one program as one semantic checkpoint. Compose mechanically linked capability calls and
  local transformations in that program; split only when the agent must make a new semantic choice,
  a separate output budget is useful, or durable recovery matters.
- Search results are candidate data. Pass source strings to content methods. Inspect selected
  documents and keep each evidence excerpt beside its source and material requirement.
- If another program may reuse a successful body, persist it immediately after fetch and before
  parsing. Load the exact selected artifact for later parser attempts and evidence rendering.
- Local parsing is local computation. When body shape is uncertain, try reasonable parser variants
  in one program and validate the task invariant—such as cardinality, unique keys, field shape, or
  source alignment—before a downstream capability call.
- Represent cumulative state with stable material keys. Start unresolved fields as unknown, keep
  independently testable fields and relations separate, and carry failures and source conflicts.
  For batches, align results to inputs and derive later inputs from validated rows.
- Preserve zero, one, or many records when a unit can yield multiple results. Derive coverage from
  the normalized rows supporting the answer.
- Across calls, save reusable data with standard Python file I/O inside the program's working
  directory. The runtime preserves these files across calls in one live session when persistence is
  enabled. The final response
  must follow the current full state.

Use these checks where they help the task. The agent judges completion from the evidence; stdout
provides a bounded view of results and progress.

## Capability and evidence basics

- Search with `sdk.search(...)` or `sdk.search.many(...)`; fuse aligned batches with
  `sdk.search.fuse_rrf(queries, results, ...)` when useful.
- Use snippets to select sources and inspected page text to support material claims. Count mirrors
  as one source family.
- Use `sdk.content.fetch(...)` or `sdk.content.fetch_many(...)` when full bodies are needed for
  local parsing or repeated checks. Use `sdk.content.grep(...)` or `sdk.content.read(...)` directly
  for selected matches or windows. Reuse text already available locally for the same evidence.
- Use deterministic Python for parsing, joins, deduplication, validation, and coverage. Keep claims
  atomic enough to validate independently: use nearby terms to locate candidates, then inspect
  whether the passage establishes
  the exact relation. Give role- or time-sensitive relations explicit scope.
- Read `sdk.capabilities()` only when a deployment mechanism or limit changes the program.

Read [the SDK contract](references/sdk-contract.md) for unfamiliar signatures, result fields,
failures, limits, and lifecycle semantics.

## Repeated and multi-call work

Batch repeated units in one invocation. Validate upstream keys and membership
before fan-out, map every available or missing result back to its input, and persist cumulative rows
only when
a later semantic checkpoint will reuse them.

For closed sets or one-to-many enumerations, read
[repeated units and record sets](references/repeated-units.md). For uncertain local parsers,
selected-artifact binding, or exact relation/conflict state, read
[optional orchestration helpers](references/orchestration.md). Adapt these helpers to the task.

For recoverable external calls, persist a `started` marker only when replay ambiguity matters, then
persist item availability or unresolved inputs before later transformation. Reconcile surviving
state from durable data before choosing which external operations to repeat.

## Return bounded observations

Collect rows first and print once. Keep one global soft budget of about 4,000 characters across
normal rows, failures, and total/shown/omitted counts. Print source-scoped excerpts and compact
summaries; keep full documents and cumulative ledgers in program state or files.

Answer the user directly once the full current state supports the request. If material fields or
enumeration scope remain unresolved and a useful next step is feasible within the task constraints,
continue researching. Report a partial or inconclusive result when useful next steps are exhausted,
a task constraint prevents continuing, or the user asks to stop; identify the remaining gaps.

## Handle failures and state loss

Broker-backed single-item methods return a result or `None`; fan-out methods return an input-aligned
list with `None` in failed positions. Check `is None` to identify failures: empty lists,
strings, or objects can be successful results. Preserve the original inputs and use
`zip(inputs, results, strict=True)` when failed-item identity matters. OpenSAC renders bounded
external-failure warnings automatically. Branch on result availability. Persist unresolved inputs
when later dataflow must remember them; use durable progress to decide which operations need another
attempt.

Public web URLs remain reusable; local IDs are session-bound. On `state_lost`, the program was not
replayed, so rebuild workspace artifacts and local-ID admission. Adapter failures occur outside the
sandbox and may leave the execution result unknown; inspect durable state before replaying.
