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

The first observation must report `execution_mode=persistent_interpreter`. If it reports another
mode or omits the mode, stop and report a configuration mismatch. Python variables, functions, and
imports survive later `agent-run` calls while `interpreter_state=ready`.

## Keep evidence boundaries intact

- Read the rendered `[sac_run]` observation, including sandbox exit code, stderr, warnings, and
  interpreter state; shell status alone is insufficient.
- Pass source strings, not result records, to content methods. Search snippets triage candidates;
  read source text used for every material claim.
- Prefer `search.many` -> `search.fuse_rrf` -> `content.passages`; use bounded `grep` and `read` for
  exact verification.
- Keep stdout compact. Warnings, stdout, stderr, and submitted output share one observation budget.
- Use deterministic Python for filtering, joins, ranking, and coverage. Treat `llm.extract_many` as
  a semantic map whose quotes still require verification.

## Use the live namespace deliberately

- Keep candidates, derived rankings, and helper functions in semantic variables such as
  `search_batches`, `shortlist`, `passages`, and `verified_evidence`.
- Reuse those variables instead of serializing them after every call. Overwrite or `del` values that
  no longer describe the active research state.
- When review is needed, print a bounded result ending with `NEXT:` and name the variables the next
  cell should reuse.
- Checkpoint expensive searches and verified evidence through `sdk.state` only at meaningful phase
  boundaries when `sdk.session.capabilities()` reports filesystem persistence. This is recovery
  state, not a copy of every temporary object; without persistence it is cell-local only.
- After an uncertain failure, inspect relevant globals and `sdk.session.usage()` before continuing;
  never replay an external operation blindly.

Read [references/stateful-research.md](references/stateful-research.md) for namespace inspection,
checkpoint, recovery, and cleanup patterns.

## End stages deliberately

- **Review needed:** print bounded evidence and end with `NEXT:`, including the next decision,
  likely capability call, and live variable names.
- **Research complete:** call `sdk.output.submit(...)` once with compact evidence and citations. Do
  not print the same result first; stop after `submitted output` appears.

If an observation reports `interpreter_state=lost` or `state_lost`, the submitted cell is never
replayed. The next call starts a clean session; recover only checkpointed work and re-admit local
sources as needed. Adapter HTTP 401/403 means credential setup failed; report it without exposing
credentials. Other adapter failures have an unknown execution outcome, so inspect before resuming.

## Load details only when needed

- Read [references/sdk-contract.md](references/sdk-contract.md) before using unfamiliar SDK methods,
  limits, failure types, or citation behavior.
- Read [references/patterns.md](references/patterns.md) for compact multi-cell exploration and
  verification examples.
- Read [references/python-recipes.md](references/python-recipes.md) for deterministic query,
  filtering, coverage, or extraction recipes.
- Read [references/advanced.md](references/advanced.md) only when the core workflow is insufficient.

Avoid unbounded namespace growth, raw result dumps, unsupported claims, and treating RRF agreement
as independent-source corroboration.
