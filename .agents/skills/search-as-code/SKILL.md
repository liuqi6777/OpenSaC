---
name: search-as-code
description: Compose evidence-grounded OpenSAC research programs through the sac_run MCP tool. Use for programmable multi-query search, document inspection, fact checking, structured extraction, persistent research state, or passage-grounded citations when sac_run is available.
---

# Search as Code

Invoke the model-visible MCP tool `sac_run(code)` and pass only a complete Python research
program. Never create, resume, or delete REST sessions. Never call `bind_context`; the MCP host
binds the current agent conversation.

Begin programs with:

```python
from opensac_sdk import BrokerError, sdk
```

## Keep the evidence boundary intact

- Treat search `ref` values and evidence locators as opaque. Never invent, edit, shorten, or
  reconstruct them.
- Use search snippets to triage sources, not to support claims about document content. Search
  metadata is sufficient only when the requested result is a discovery list.
- Read the passage used for each material claim. Cite only a non-empty passage that returned a
  locator, and preserve `locator.model_dump(mode="json")` losslessly.
- Treat a locator as proof that a passage is bound to a retrieved document, not as proof that the
  source is credible or the claim is true. Prefer primary sources and corroborate disputed claims.
- Inspect typed item failures and `BrokerError`. Empty hits and zero matches are successful
  results. After a final failure, change the query, source, or candidate instead of repeating it.
- Keep stdout small. Stdout, stderr, and submitted output share one observation budget, and noisy
  progress can hide the final submitted result.

## Run a bounded research stage

Make one `sac_run` call carry a complete stage: search, rank, locate, verify, persist, and report.

1. **Frame:** write explicit constraints and derive a stable `research_id` from the exact task and
   constraint specifications. Store state under `runs/<research_id>/` so follow-up tasks in the
   same conversation cannot reuse unrelated evidence.
2. **Survey:** deduplicate query strings before `sdk.search.many`. Use 2-4 focused variants for a
   known entity and 6-12 only for ambiguous or rare-clue discovery. Fuse with RRF, retain leaders
   from every query, and cap the persisted pool.
3. **Locate:** order refs by the current stage's RRF rank, then historical score. Call
   `grep_report` on small ranked batches; grep fetches and caches documents, so do not send the
   whole accumulated pool blindly.
4. **Verify:** read around useful 1-indexed match lines, check every constraint separately, and
   record attempted `(constraint, ref)` pairs so the next stage advances to new candidates.
5. **Submit:** call `sdk.output.submit` only when every material claim has a verified locator.
   Submit compact evidence excerpts and the exact locators used.

Use ordinary Python for joins, deduplication, regex, ranking, dates, set arithmetic, and coverage.
Use `sdk.llm.extract_many` only when a configured pipeline model is needed for semantic work.
JSON Schema validates shape, not truth: require an evidence quote and verify in Python that it is
present in the source passage.

## Persist and recover correctly

Workspace files, refs, and locators survive later calls only while this conversation's OpenSAC
session remains live; Python variables do not. Reload state at the start of every program.

If `sac_run` returns `state_lost`, the submitted program was not replayed. Treat every prior
workspace file, ref, and locator as gone. Start the next stage from clean state and do not resubmit
the same program blindly.

An observation such as `[sac_run] OpenSAC request failed` or a tool-level timeout comes from the
adapter, outside the sandbox, so the program cannot catch it as `BrokerError`. Its execution
outcome may be unknown because the model-visible tool supplies no execution ID. Do not replay the
same program blindly. Use the next call as a small recovery stage that inspects the task namespace
and usage, then resume only missing work. If repeated adapter failures prevent inspection, report
OpenSAC as unavailable. Use another authorized research path only with explicit provenance, and
never invent an OpenSAC locator for it.

## Load details only when needed

- Read [references/sdk-contract.md](references/sdk-contract.md) before using unfamiliar SDK
  methods, fields, failure types, limits, or citation behavior.
- Read [references/patterns.md](references/patterns.md) before a multi-turn or multi-constraint
  investigation, or before using checked semantic extraction.

Avoid printing raw result objects, opaque refs, or whole pages, stopping after one constraint, or
treating RRF agreement as independent-source corroboration.
