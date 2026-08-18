---
name: search-as-code-cli
description: Run evidence-grounded OpenSAC research programs through the local opensac agent-run CLI. Use when Codex, Claude Code, or another shell-capable agent needs programmable multi-query search, document inspection, fact checking, checked extraction, persistent research state, or passage-grounded citations without MCP.
---

# Search as Code CLI

Pipe one complete Python research stage to `opensac agent-run` through the current agent's shell:

```bash
opensac agent-run <<'OPENSAC_PY'
from opensac_sdk import BrokerError, sdk

# Replace this probe with a complete stage from references/patterns.md.
print(sdk.session.usage())
OPENSAC_PY
```

Use a quoted heredoc delimiter so the shell cannot expand generated Python. Put only the program
on stdin; never encode it into a shell argument. Let the host bind the current conversation.
Never expose, print, or override host conversation identity. Never create or manage REST sessions
or call OpenSAC endpoints directly.

If the command is unavailable or reports `context_*` or `configuration_error`, stop and report the
setup problem.

## Keep the command and evidence boundaries intact

- Read the rendered `[sac_run]` observation instead of trusting only the shell status. A reported
  sandbox `exit_code`, stderr, adapter failure, or missing submitted output can change the next
  stage.
- Treat search refs and evidence locators as opaque. Never invent, edit, shorten, or reconstruct
  them.
- Use search snippets to triage sources, not to support document-content claims. Search metadata
  is sufficient only when the requested result is a discovery list.
- Read the passage used for every material claim. Cite only a non-empty passage that returned a
  locator, and preserve `locator.model_dump(mode="json")` losslessly.
- Treat a locator as proof that a passage is bound to a retrieved document, not as proof that its
  source is credible or its claim is true. Prefer primary sources and corroborate disputed claims.
- Inspect typed item failures and `BrokerError`. Empty hits and zero matches are successful
  results. After a final failure, change the query, source, or candidate instead of repeating it.
- Keep stdout compact. Stdout, stderr, and submitted output share one observation budget.

## Run a bounded research stage

Make one CLI execution carry a complete stage: search, rank, locate, verify, persist, and report.

1. **Frame:** define explicit constraints and derive a stable `research_id` from the exact task,
   stable requirements, and source policy. Store state under `runs/<research_id>/`.
2. **Survey:** deduplicate queries before `sdk.search.many`. Use 2-4 focused variants for a known
   entity and 6-12 only for ambiguous discovery. Fuse with RRF, retain leaders from every query,
   and cap the persisted pool.
3. **Locate:** order refs by current-stage RRF rank, then historical score. Call `grep_report` on
   small ranked batches; do not send the whole accumulated pool blindly.
4. **Verify:** read around useful 1-indexed match lines, verify every constraint separately, and
   record attempted `(constraint, ref)` pairs so later stages advance to new candidates.
5. **Submit:** call `sdk.output.submit` only when every material claim has a verified locator.
   Submit compact excerpts and the exact locators used.

Use ordinary Python for joins, deduplication, regex, ranking, dates, set arithmetic, and coverage.
Use `sdk.llm.extract_many` only when a configured pipeline model is needed for semantic work.
JSON Schema validates shape, not truth: require an evidence quote and verify in Python that it is
present in the source passage.

## Persist and recover correctly

Workspace files, refs, and locators survive later `agent-run` calls only while the host-bound
OpenSAC session remains live; Python variables do not. Reload state at the start of every program.

If the observation reports `state_lost`, the submitted program was not replayed. Treat all prior
workspace files, refs, and locators as gone. Start the next stage from clean state and do not
resubmit the same program blindly.

An adapter `HTTP 401` or `HTTP 403` observation is a host credential setup failure. Stop without
retrying, and report it without printing or embedding any credential. Other adapter failures and
timeouts occur outside the sandbox, so the program cannot catch them as `BrokerError`. Their
execution outcome may be unknown because `agent-run` accepts no execution ID. Do not replay the
same program blindly. Use the next invocation as a small recovery stage that inspects the task
namespace and usage, then resume only missing work. If repeated adapter failures prevent
inspection, report OpenSAC as unavailable and never invent an OpenSAC locator.

## Load details only when needed

- Read [references/sdk-contract.md](references/sdk-contract.md) before using unfamiliar SDK
  methods, fields, failure types, limits, or citation behavior.
- Read [references/patterns.md](references/patterns.md) before a multi-turn or multi-constraint
  investigation, or before checked semantic extraction.

Avoid printing raw result objects, opaque refs, or whole pages, stopping after one constraint, or
treating RRF agreement as independent-source corroboration.
