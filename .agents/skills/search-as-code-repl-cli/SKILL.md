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
`configuration_error`, stop.
`opensac agent-run` is the outer adapter command. Keep the heredoc body as plain Python; never place
`sac_run(...)` or another `agent-run` command inside it.

The first observation must report `execution_mode=persistent_interpreter`. If it reports another
mode or omits it, stop and report a configuration mismatch. Continue reusing the live interpreter
only while `interpreter_state=ready`; `not_started` has no reusable Python namespace yet.

Treat the persistent interpreter as a capability surface with optional live memory, not a prescribed
research workflow. Choose the query count, capability sequence, cell split, variable names, cleanup,
and checkpoint design from the task and observations. Use one cell or many as useful.

## Use OpenSAC and inspect evidence

- Read the rendered `[sac_run]` observation, including sandbox exit code, stderr, warnings, and
  interpreter state; shell status alone is insufficient.
- Pass source strings, not result records, to content methods. Web URLs are reusable; local IDs
  remain bound to this session.
- Use `sdk.search(...)` or `sdk.search.many(...)` for retrieval; optionally fuse results with
  `sdk.search.fuse_rrf(...)`. Use `sdk.content.passages(...)`, `sdk.content.grep(...)`, and
  `sdk.content.read(...)` in any combination that fits the evidence need.
- Use search snippets for triage, not document claims. Inspect the source text used for each material
  claim. Treat `sdk.llm.extract_many(...)` output as a transformation of supplied text, not new
  evidence, and validate returned quotes against that text.
- Keep stdout compact. Warnings, stdout, stderr, and submitted output share one observation budget.

## Use live and durable state when useful

- Python variables, functions, imports, and assignments completed before an ordinary exception
  survive later cells while the interpreter remains ready. Reuse, replace, or discard them as the
  research requires; no variable naming or cleanup convention is required.
- Treat interpreter memory and filesystem persistence as independent mechanisms. Use `sdk.state`
  for optional recovery checkpoints only when
  `sdk.session.capabilities()["mechanisms"]["persistence"]` is enabled. Choose what, when, and how
  to checkpoint based on recomputation cost; do not mirror every live object.
- After an uncertain failure, inspect relevant globals and `sdk.session.usage()` in a read-only cell
  before repeating an external operation. Never replay it blindly.

Read [references/stateful-research.md](references/stateful-research.md) for namespace inspection,
optional checkpoint, recovery, and cleanup examples.

## Return observations and results

- Use stdout for compact intermediate observations. Include any source, text, or live variable names
  needed for the next judgment; no stage marker is required.
- Call `sdk.output.submit(...)` once for the completed research result. Do not print the same final
  payload first; stop after `submitted output` appears.

If an observation reports `interpreter_state=lost` or `state_lost`, the submitted cell is never
replayed. The next call starts a clean session. Restore a trustworthy checkpoint if one exists,
re-admit local sources, and recompute whatever the evidence still requires. Adapter HTTP 401/403
means credential setup failed; report it without exposing credentials. Other adapter failures have
an unknown execution outcome, so inspect before resuming.

## Load details and examples only when useful

- Read [references/sdk-contract.md](references/sdk-contract.md) before using unfamiliar SDK methods,
  limits, failure types, or citation behavior.
- Read [references/patterns.md](references/patterns.md) for adaptable multi-cell examples.
- Read [references/python-recipes.md](references/python-recipes.md) for optional Python fragments.
- Read [references/advanced.md](references/advanced.md) only when the core workflow is insufficient.

Treat every example as a starting point, not a required cell sequence. Adapt or ignore its query
count, ordering, cell boundaries, variable names, source policy, and checkpoint schema.
