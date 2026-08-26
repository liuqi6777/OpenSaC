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

Treat the persistent interpreter as a capability surface with optional live memory, not a prescribed
research workflow. Choose the query count, capability sequence, cell split, variable names, cleanup,
and checkpoint design from the task and observations. Use one cell or many as useful.

## Use OpenSAC and inspect evidence

- Pass source strings, not result records, to content methods. Web URLs are reusable; local IDs
  remain bound to this session.
- Use `sdk.search(...)` or `sdk.search.many(...)` for retrieval; optionally fuse results with
  `sdk.search.fuse_rrf(...)`. Use `sdk.content.passages(...)`, `sdk.content.grep(...)`, and
  `sdk.content.read(...)` in any combination that fits the evidence need.
- Use search snippets for triage, not document claims. Inspect the source text used for each material
  claim. Treat `sdk.llm.extract_many(...)` output as a transformation of supplied text, not new
  evidence, and validate returned quotes against that text.
- Keep stdout bounded. Warnings, stdout, stderr, and submitted output share one observation budget.

## Use live and durable state when useful

- Python variables, functions, imports, and assignments completed before an ordinary exception
  survive later cells while the interpreter remains ready. Reuse, replace, or discard them as the
  research requires; no variable naming or cleanup convention is required.
- Treat interpreter memory and filesystem persistence as independent mechanisms. Use `sdk.state`
  for optional recovery checkpoints only when
  `sdk.session.capabilities()["mechanisms"]["persistence"]` is enabled. Choose what, when, and how
  to checkpoint based on recomputation cost; do not mirror every live object.
- After an uncertain tool failure, inspect the relevant globals and `sdk.session.usage()` in a
  read-only cell before repeating an external operation. Never replay it blindly.

Read [references/stateful-research.md](references/stateful-research.md) for namespace inspection,
optional checkpoint, recovery, and cleanup examples.

## Return observations and results

- Use stdout for compact intermediate observations. Include any source, text, or live variable names
  needed for the next judgment; no stage marker is required.
- Call `sdk.output.submit(...)` once for the completed research result. Do not print the same final
  payload first; stop calling `sac_run` after `submitted output` appears.

If an observation reports `interpreter_state=lost` or `state_lost`, the submitted cell is never
replayed. The next call starts a clean session. Restore a trustworthy checkpoint if one exists,
re-admit local sources, and recompute whatever the evidence still requires.

## Load details and examples only when useful

- Read [references/sdk-contract.md](references/sdk-contract.md) before using unfamiliar SDK methods,
  limits, failure types, or citation behavior.
- Read [references/patterns.md](references/patterns.md) for adaptable multi-cell examples.
- Read [references/python-recipes.md](references/python-recipes.md) for optional Python fragments.
- Read [references/advanced.md](references/advanced.md) only when the core workflow is insufficient.

Treat every example as a starting point, not a required cell sequence. Adapt or ignore its query
count, ordering, cell boundaries, variable names, source policy, and checkpoint schema.
