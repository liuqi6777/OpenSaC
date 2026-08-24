---
name: search-as-code-repl
description: Run evidence-grounded OpenSAC research through sac_run when the host is explicitly configured for the experimental persistent_interpreter mode. Use only when invoked as $search-as-code-repl; use search-as-code for ordinary process-per-call sessions.
---

# Search as Code REPL

Invoke `sac_run(code)` with one complete Python cell. The MCP host binds the conversation and owns
the OpenSAC session; never create, display, or delete REST sessions or kernel identifiers.

Begin the first cell with:

```python
from opensac_sdk import BrokerError, sdk
```

The first observation must report `execution_mode=persistent_interpreter`. If it reports another
mode or omits the mode, stop and report a configuration mismatch. Python variables, functions, and
imports survive later `sac_run` calls while `interpreter_state=ready`.

## Keep evidence boundaries intact

- Pass source strings, not result records, to content methods. Web URLs are reusable; local IDs
  remain bound to this session.
- Search snippets triage candidates. Read the source text used for every material claim.
- Prefer `search.many` -> `search.fuse_rrf` -> `content.passages`; use `grep` and `read` for exact
  verification and deliberate context expansion.
- Keep stdout bounded. Warnings, stdout, stderr, and submitted output share one observation budget.
- Use deterministic Python for filtering, joins, ranking, and coverage. Treat `llm.extract_many` as
  a semantic map whose quotes still require verification.

## Use the live namespace deliberately

- Keep short-lived candidates, derived rankings, and helper functions in semantic variables such as
  `search_batches`, `shortlist`, `passages`, and `verified_evidence`.
- Reuse those variables in later cells instead of serializing them after every call. Overwrite or
  `del` values that no longer describe the current research state.
- When review is needed, print a compact observation ending with `NEXT:`. Name the variables the
  next cell should reuse, but do not dump their full contents.
- At meaningful phase boundaries, checkpoint expensive searches and verified evidence through
  `sdk.state` when `sdk.session.capabilities()` reports filesystem persistence. This is recovery
  state, not a copy of every temporary Python object; without persistence it is cell-local only.
- After an uncertain tool failure, inspect the relevant globals and `sdk.session.usage()` in a
  small cell before continuing. Never replay an external operation blindly.

Read [references/stateful-research.md](references/stateful-research.md) for namespace inspection,
checkpoint, recovery, and cleanup patterns.

## End stages deliberately

- **Review needed:** print bounded evidence and end with `NEXT:`, including the next decision,
  likely capability call, and the live variable names it depends on.
- **Research complete:** call `sdk.output.submit(...)` once with compact evidence and citations. Do
  not print the same result first; stop calling `sac_run` after `submitted output` appears.

If an observation reports `interpreter_state=lost` or `state_lost`, the submitted cell is never
replayed. The next call starts a clean session; recover only checkpointed work and re-admit local
sources as needed.

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
