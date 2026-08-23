---
name: search-as-code
description: Compose evidence-grounded OpenSAC research programs through the sac_run MCP tool. Use for programmable multi-query search, document inspection, fact checking, structured extraction, workspace-backed research state, or URL-cited results when sac_run is available.
---

# Search as Code

Invoke the model-visible MCP tool `sac_run(code)` and pass only a complete Python research
program. Never create, resume, or delete REST sessions. The MCP host binds the current agent
conversation through request metadata.

Begin programs with:

```python
from opensac_sdk import BrokerError, sdk
```

## Keep the evidence boundary intact

- Sources are URL or local-ID strings. Web content accepts bounded public HTTP(S) URLs directly;
  local IDs still require search admission. Pass strings, never result records, to content.
- Use search snippets to triage sources, not to support claims about document content. Search
  metadata is sufficient only when the requested result is a discovery list.
- Prefer `search.many` -> `search.fuse_rrf` -> `content.passages` for semantic discovery. Inspect
  returned text; use `grep` and `read` for exact strings and deliberate context expansion.
- Apply task-specific domain policy in `fuse_rrf` before its final limit.
- Read the text used for each material claim. Output citations are optional, unverified URL/source
  labels; prefer primary sources and corroborate disputed claims.
- Inspect item failure records and `BrokerError`. Empty hits and zero matches are successful
  results. After a final failure, change the query, source, or candidate instead of repeating it.
- Keep stdout small. Stdout, stderr, and submitted output share one observation budget, and noisy
  progress can hide the final submitted result.

## End stages deliberately

- **Review needed:** print bounded results and end with `NEXT:`, naming the model decision and
  likely next operation. Include bounded URL/domain/title candidates so the next call can reuse URLs.
- **Research complete:** call `sdk.output.submit(...)` once with compact evidence and citations;
  do not print them first. After `submitted output` appears, stop calling `sac_run` and answer.

A final research result must use `submit`; stdout is not a substitute.

## Split on model judgment

Keep each `sac_run` call short. Pause when titles, snippets, or passages must be understood before
choosing the next query, source, pattern, or rule. An exploratory search-only stage is valid; do not
append grep merely for completeness. Continue when an explicit rule determines the next inputs:
search can fuse/filter, while known sources and patterns can grep/read in one program.

Frame constraints and source policy first. Use 2-4 queries for a known entity and 6-12 only for
ambiguous discovery. Fuse a bounded shortlist, rank passages across its sources, inspect the original
passage text, and submit only after every material claim is supported by inspected text. Use bounded grep/read calls
when verification depends on an exact spelling or more surrounding lines.

## Orchestrate with Python

Prefer deterministic Python for query construction, filtering, joins, ranking, and coverage.
Treat `sdk.llm.extract_many` as a semantic map, not an inner tool-calling agent: validate its
quotes, then make at most a bounded follow-up capability call.

## Use the workspace as program memory

The session workspace is program-to-program memory through `sdk.state`; no `sdk.workspace` API
exists. Stdout guides the control model, while workspace contents guide the next program.
Observations show artifact paths, not their contents.

For multi-call work, derive `runs/<research_id>/` from the task, requirements, and source policy.
At each stage start, list and load its manifest, bounded candidate pool, verified evidence, and
attempted `(constraint, source)` pairs. Before ending with `NEXT:`, persist every useful update and
confirm the expected artifact paths appear in the observation. Submit from the evidence ledger
only after coverage is complete. Python variables do not survive a call.

Public web URLs remain reusable across calls and sessions; local IDs remain session-bound. If
`sac_run` returns `state_lost`, the submitted program was not replayed; rebuild workspace state and
local-source admission, then resume only missing work.

Adapter failures and tool timeouts occur outside the sandbox, so their execution outcome may be
unknown. Do not replay the same program blindly. Inspect the task namespace and usage in one small
recovery stage, then resume only missing work. If inspection repeatedly fails, report OpenSAC as
unavailable.

## Load details only when needed

For installed-version details, print one method's `__doc__`; it makes no broker call. Never dump
all runtime docs because they share the stdout observation budget.

- Read [references/sdk-contract.md](references/sdk-contract.md) before using unfamiliar SDK
  core/helper methods, fields, failure types, limits, or citation behavior.
- Read [references/advanced.md](references/advanced.md) only when a core workflow is insufficient.
- Read [references/patterns.md](references/patterns.md) when a weaker model needs separate compact
  examples for exploration and verification/submission.
- Read [references/python-recipes.md](references/python-recipes.md) when query variants should be
  generated systematically, search results filtered locally, coverage aggregated, or extraction
  output used to drive a bounded follow-up action.
- Read [references/stateful-research.md](references/stateful-research.md) only when a task must
  continue across multiple `sac_run` calls and needs a workspace-backed candidate/evidence ledger.

Avoid printing raw result objects, unbounded source lists, or whole pages, stopping after one
constraint, or treating RRF agreement as independent-source corroboration.
