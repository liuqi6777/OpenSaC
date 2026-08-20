---
name: search-as-code-cli
description: Run evidence-grounded OpenSAC Python research through the local agent-run CLI for Codex, Claude Code, or another shell-capable agent. Use for multi-query search, document inspection, fact checking, extraction, workspace state, or URL-cited results without MCP.
---

# Search as Code CLI

Pipe one Python research stage to `opensac agent-run`:

```bash
opensac agent-run <<'OPENSAC_PY'
from opensac_sdk import BrokerError, sdk

# Replace this probe with a complete stage from references/patterns.md.
print(sdk.session.usage())
OPENSAC_PY
```

Use a quoted heredoc to prevent shell expansion; send the program only on stdin, never in a shell
argument. Let the host bind the conversation; never expose, print, or override its identity.
Never create or manage REST sessions or call OpenSAC endpoints directly.

If unavailable or reporting `context_*` or `configuration_error`, stop and report setup failure.

## Keep the command and evidence boundaries intact

- Read the rendered `[sac_run]` observation instead of trusting only the shell status. A reported
  sandbox `exit_code`, stderr, adapter failure, or missing submitted output can change the next
  stage.
- Sources are URL or local-ID strings. Web content accepts bounded public HTTP(S) URLs directly;
  local IDs still require search admission. Pass strings, never result records, to content.
- Use search snippets to triage sources, not to support document-content claims. Search metadata
  is sufficient only when the requested result is a discovery list.
- Prefer `search.many` -> `search.fuse_rrf` -> `content.passages` for semantic discovery. Inspect
  returned text; use `grep_report` and `read` for exact strings and deliberate context expansion.
- Read the text used for each material claim. Output citations are optional, unverified URL/source
  labels; prefer primary sources and corroborate disputed claims.
- Inspect item failure records and `BrokerError`. Empty hits and zero matches are successful
  results. After a final failure, change the query, source, or candidate instead of repeating it.
- Keep stdout compact. Stdout, stderr, and submitted output share one observation budget.

## End stages deliberately

- **Review needed:** print bounded results and end with `NEXT:`, naming the model decision and
  likely next operation. Include bounded URL/domain/title candidates so the next call can reuse URLs.
- **Research complete:** call `sdk.output.submit(...)` once with compact evidence and citations;
  do not print them first. After `submitted output` appears, stop calling `agent-run` and answer.

A final research result must use `submit`; stdout is not a substitute.

## Split on model judgment

Keep each `agent-run` program short. Pause when titles, snippets, or passages must be understood
before choosing the next query, source, pattern, or rule. An exploratory search-only stage is valid;
do not append grep merely for completeness. Continue when an explicit rule determines the next
inputs: search can fuse/filter, while known sources and patterns can grep/read in one program.

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

For multi-call work, derive a stable `research_id` and use `runs/<research_id>/`. At each stage
start, list and load its manifest, bounded candidate pool, verified evidence, and attempted
`(constraint, source)` pairs. Before ending with `NEXT:`, persist every useful update and confirm the
expected artifact paths appear in the observation. Submit from the evidence ledger only after
coverage is complete. Python variables do not survive a call.

Public web URLs remain reusable across calls and sessions; local IDs remain session-bound. If the
observation reports `state_lost`, the submitted program was not replayed; rebuild workspace state
and local-source admission, then resume only missing work.

An adapter `HTTP 401` or `HTTP 403` means host credential setup failed; stop.
Report it without printing or embedding any credential. Other adapter failures occur outside the
sandbox, so execution outcome may be unknown. Do not replay blindly: inspect task state and usage
once, resume missing work, or report OpenSAC as unavailable.

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
  continue across multiple `agent-run` calls and needs a workspace-backed candidate/evidence
  ledger.

Avoid printing raw result objects, unbounded source lists, or whole pages, stopping after one
constraint, or treating RRF agreement as independent-source corroboration.
