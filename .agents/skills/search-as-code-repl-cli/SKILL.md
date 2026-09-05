---
name: search-as-code-repl-cli
description: Run evidence-grounded OpenSAC research through agent-run when the CLI is explicitly configured for the experimental persistent_interpreter mode. Use when explicitly invoked as $search-as-code-repl-cli; use search-as-code-cli for ordinary process-per-call sessions.
---

# Search as Code REPL CLI

Pipe one complete Python cell to `opensac agent-run`:

```bash
opensac agent-run <<'PY'
from opensac_sdk import sdk

capabilities = sdk.capabilities()
print(f"capabilities_available={capabilities is not None}")
PY
```

Use a quoted heredoc to send the Python body on stdin. Run `opensac agent-run` in the outer shell;
use SDK calls and local Python computation inside the body. The adapter owns conversation identity,
REST sessions, and execution identifiers; let it manage their lifecycle and keep those details
private.
In an OpenSaC source checkout, `uv run opensac agent-run` provides the same stdin interface when
`opensac` is unavailable on `PATH`. When both launchers are unavailable, or the command reports
`context_*` or `configuration_error`, report the setup failure and wait for setup to be corrected.

This skill is available only when the host is explicitly configured for
`persistent_interpreter`; observations contain program output and failure information. Reuse the
live
interpreter until an explicit `state_lost` or `interpreter_lost` error reports that it was
discarded.

Choose queries, sources, stages, stopping criteria, and artifact layouts to fit the user's task.
This skill provides SDK contracts and dataflow checks for implementing that research strategy.

## Core orchestration contract

- Treat one cell as one semantic checkpoint. Compose mechanically linked capability calls and local
  transformations in that cell; split only when the agent must make a new semantic choice, a
  separate
  output budget is useful, or recovery/debugging matters.
- Search results are candidate data. Pass source strings to content methods. Inspect selected
  documents and keep each evidence excerpt beside its source and material requirement.
- If another cell may reuse a successful body, keep the exact returned document in live memory while
  the interpreter is ready. Persist it before parsing when recovery or reuse beyond live state
  matters.
  Reuse that exact body for later parser attempts and evidence rendering.
- Local parsing is local computation. When body shape is uncertain, try reasonable parser variants
  in one cell and validate the task invariant—such as cardinality, unique keys, field shape, or
  source
  alignment—before a downstream capability call.
- Represent cumulative state with stable material keys. Start unresolved fields as unknown, keep
  independently testable fields and relations separate, and carry failures and source conflicts.
  For batches, align results to inputs and derive later inputs from validated rows.
- Preserve zero, one, or many records when a unit can yield multiple results. Derive coverage from
  the normalized rows supporting the answer.
- Across cells, the final response must follow the current full live and durable state.

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
- Read `sdk.capabilities()` only when a deployment mechanism or limit changes the cell.

Read [the SDK contract](references/sdk-contract.md) for unfamiliar signatures, result fields,
failures, limits, and lifecycle semantics.

## Repeated and multi-cell work

Batch repeated units in one invocation. Validate upstream keys and membership
before fan-out, map every available or missing result back to its input, and retain cumulative rows
only when a
later semantic checkpoint will reuse them.

For closed sets or one-to-many enumerations, read
[repeated units and record sets](references/repeated-units.md). For uncertain local parsers,
selected-artifact binding, or exact relation/conflict state, read
[optional orchestration helpers](references/orchestration.md). Adapt these helpers to the task.

Python variables, functions, imports, and completed assignments survive ordinary exceptions while
the interpreter remains ready. Treat live memory and files as independent mechanisms. Use standard
Python file I/O in the cell's working directory for reusable checkpoints; file retention follows
`mechanisms.persistence`. After state loss, reuse only checkpoints that remain available and valid.

For recoverable external calls, persist a `started` marker only when replay ambiguity matters, then
persist item availability or unresolved inputs before later transformation. After an uncertain
adapter failure, inspect relevant globals and durable rows before repeating an external operation.

## Return bounded observations

Collect rows first and print once. Keep one global soft budget of about 4,000 characters across
normal rows, failures, and total/shown/omitted counts. Print source-scoped excerpts and compact
summaries; keep full documents and cumulative ledgers in program state or files.

Answer the user directly once the full current state supports the request. If material fields or
enumeration scope remain unresolved and a useful next step is feasible within the task constraints,
continue researching. Report a partial or inconclusive result when useful next steps are exhausted,
a task constraint prevents continuing, or the user asks to stop; identify the remaining gaps.

## Handle interpreter and adapter failures

Read the rendered observation, including structured OpenSAC warnings and errors, together with
the shell exit status. Broker-backed single-item methods return a result or `None`; fan-out methods
return
an input-aligned list with `None` in failed positions. Check `is None` to identify failures: empty
lists, strings, and objects can be successful results. Preserve the original inputs and use
`zip(inputs, results, strict=True)` when failed-item identity matters. OpenSAC renders bounded
external-failure warnings automatically. Branch on result availability. Persist unresolved inputs
when later dataflow must remember them.

If an observation reports `state_lost` or `interpreter_lost`, the cell is not replayed and the next
invocation starts clean. Restore trustworthy workspace data if present, re-admit local IDs, and
reuse
public URLs only when permitted. Adapter failures occur outside the sandbox and may leave execution
result unknown; inspect surviving state before replaying. Adapter `HTTP 401` or `HTTP 403` means
host
credential setup failed; report the failure code and keep credentials private.
