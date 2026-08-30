---
name: search-as-code-repl
description: Run evidence-grounded OpenSAC research through sac_run when the host is explicitly configured for the experimental persistent_interpreter mode. Use only when invoked as $search-as-code-repl; use search-as-code for ordinary process-per-call sessions.
---

# Search as Code REPL

Invoke `sac_run(code)` with one complete Python cell. The MCP host binds the conversation and owns
the OpenSAC session; never create, display, or delete REST sessions or kernel identifiers.
`sac_run` is the outer adapter tool, not a Python API inside the sandbox. Put only the cell body in
the `code` argument; never call `sac_run` from inside that cell. Import `BrokerError` and `sdk` from
`opensac_sdk` when needed.

The first observation must report `execution_mode=persistent_interpreter`. If it reports another
mode or omits it, stop and report a configuration mismatch. Reuse the live interpreter only while
`interpreter_state=ready`; `not_started` has no reusable Python namespace yet.

Choose the research strategy yourself. This skill teaches how to encode that strategy as OpenSAC
code; it does not prescribe query counts, source choices, cell stages, stopping rules, live-variable
names, or one workspace schema.

## Core orchestration contract

- Treat one cell as one semantic checkpoint. Compose mechanically linked capability calls and local
  transformations in that cell; split only when the agent must make a new semantic choice, a separate
  output budget is useful, or recovery/debugging matters.
- Search outcomes are candidate data. Pass source strings, not result records, to content methods.
  Fetch only selected sources, inspect returned bodies locally, and keep each evidence excerpt beside
  its source and material requirement.
- If another cell may reuse a successful body, keep the exact returned document in live memory while
  the interpreter is ready. Persist it before parsing when recovery or reuse beyond live state matters.
  Reuse that exact body instead of fetching again merely to try another parser or render evidence.
- Local parsing is local computation. When body shape is uncertain, try reasonable parser variants
  in one cell and validate the task invariant—such as cardinality, unique keys, field shape, or source
  alignment—before a downstream capability call.
- Represent cumulative state with stable material keys. Start unresolved fields as unknown, keep
  independently testable fields and relations separate, and carry failures and source conflicts.
  For batches, align outcomes to inputs and derive later inputs from validated rows rather than
  retyping an anticipated list.
- Preserve zero, one, or many records when a unit can yield multiple results. Do not collapse a
  record set to one convenient match; derive coverage from normalized rows supporting the answer.
- Across cells, the final response must follow the current full live and durable state, not a
  remembered answer or a subset printed in an earlier observation.

These are dataflow invariants, not a required pipeline. Completion is an agent-level evidence
judgment, not a code token. stdout is a bounded audit projection; code does not need to print a
completion label or every final row.

## Capability and evidence basics

- Search with `sdk.search(...)` or `sdk.search.many(...)`; optionally fuse results.
- Material claims require inspected page text. Snippets help select sources but are not claim
  evidence. Mirrors count as one source family.
- Make `sdk.content.fetch(...)` or `sdk.content.fetch_many(...)` the first content call for a
  selected source. Do not use `sdk.content.grep(...)` or `sdk.content.read(...)` merely to relocate
  text already present in a fetched body, and never pass an unfetched source to content.
- Use deterministic Python for parsing, joins, deduplication, validation, and coverage. Keep claims
  atomic enough to validate independently: nearby terms are candidates for a relation, not proof of
  it. Give role- or time-sensitive relations explicit scope.
- Read `sdk.capabilities()` only when a deployment mechanism or limit changes the cell.

Read [the SDK contract](references/sdk-contract.md) for unfamiliar signatures, result fields,
failures, limits, and lifecycle semantics.

## Repeated and multi-cell work

Batch repeated units instead of using one `sac_run` per row. Validate upstream keys and membership
before fan-out, map every success or failure back to its input, and retain cumulative rows only when a
later semantic checkpoint will reuse them.

For closed sets or one-to-many enumerations, read
[repeated units and record sets](references/repeated-units.md). For uncertain local parsers,
selected-artifact binding, or exact relation/conflict state, read
[optional orchestration helpers](references/orchestration.md). These helpers are adaptable checks,
not mandatory schemas.

Python variables, functions, imports, and completed assignments survive ordinary exceptions while
the interpreter remains ready. Treat live memory and `sdk.workspace` as independent mechanisms; use
the workspace when durable recovery or reuse after state loss saves meaningful external work.

For recoverable external calls, persist a `started` marker only when replay ambiguity matters, then
persist item outcomes before later transformation. After an uncertain adapter failure, inspect
relevant globals and durable rows before repeating an external operation.

## Return bounded observations

Collect rows first and print once. Keep one global soft budget of about 4,000 characters across
normal rows, failures, and total/shown/omitted counts. Every shown evidence row keeps its source.
Do not print raw result lists, complete documents, live namespaces, or workspace ledgers.

Cell output need not say whether research should continue. Once the full current state supports the
request, answer the user directly; if material fields or enumeration scope remain unresolved, report
a partial or inconclusive result. No finalization-only cell is needed merely to announce completion.

## Handle interpreter and adapter failures

For `search.many`, branch on `status == "success"`; failed rows use `outcome.error`, while an empty
successful result is valid. Carry failures rather than hard-coding zero failures. A caught
`BrokerError` leaves that work unresolved; return bounded failure data and let the next agent
decision choose recovery.

If an observation reports `interpreter_state=lost` or `state_lost`, the cell is not replayed and the
next invocation starts clean. Restore trustworthy workspace data if present, re-admit local IDs, and
reuse public URLs only when permitted. Adapter failures occur outside the sandbox and may leave
execution outcome unknown; inspect surviving state before replaying.

Treat every referenced helper as a starting point, not a required pipeline.
