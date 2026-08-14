---
name: search-as-code-cli
description: Run evidence-grounded research as Python programs through the local OpenSAC agent-run CLI. Use when Codex or Claude Code has shell access and needs programmable search, document inspection, checked extraction, persistent research state, or trusted citations without MCP.
---

# Search as Code CLI

Pipe each Python research program to `opensac agent-run`. Never create or manage REST sessions,
pass conversation identifiers, or call OpenSAC endpoints directly. If the command is unavailable
or reports a `context_*` or `configuration_error`, stop and report the setup problem.

```bash
opensac agent-run <<'OPENSAC_PY'
from opensac_sdk import BrokerError, sdk

batches = sdk.search.many(
    ['"exact phrase" narrowing terms', "same clue alternate wording"],
    limit_per_query=10,
    concurrency=4,
)
fusion = sdk.search.fuse_rrf(batches, k=60)
refs = [candidate.ref for candidate in fusion.candidates[:20]]
report = sdk.content.grep_report(refs, r"target phrase", context=2)

for match in report.matches[:3]:
    passage = sdk.content.read(
        [match.ref], offset=max(1, match.line - 8), limit=30, max_chars=16_000
    )[0]
    if passage.failure is None and passage.locator is not None:
        print(passage.ref, passage.text[:500])
OPENSAC_PY
```

Use a quoted heredoc delimiter so the shell does not expand generated Python. Put only the program
on stdin; do not encode it into a shell argument.

## Core primitives

- `sdk.search(query, limit=10, offset=0)`
- `sdk.search.many(queries, limit_per_query=10, offset=0, concurrency=5)`
- `sdk.search.fuse_rrf(batches, weights=None, k=60, limit=None)`
- `sdk.content.grep_report(refs, pattern, context=0, max_matches_per_ref=20)`
- `sdk.content.read(refs, offset=1, limit=200, max_chars=100_000)`
- `sdk.content.get_many(refs)` and `sdk.content.snippets(query, refs, ...)`
- `sdk.llm.extract_many(items, instruction=..., schema=..., concurrency=4)`
- `sdk.state.merge_jsonl`, `read_jsonl`, `write_json`, `read_json`, and `exists`
- `sdk.session.usage()`
- `sdk.output.submit(output, citations=[{"ref": ref, "locator": locator}])`

Search returns opaque `ref` values. Never invent, edit, or reconstruct them. Search snippets are
previews, not final evidence. Read the passages used for the answer and preserve their returned
locators exactly. Never cite text whose locator is missing or reports
`evidence_capacity_exhausted`.

Normal failures are aligned and typed: inspect `failure.code`, `message`, `retryable`, and
`attempts`. Empty hits and zero matches are successful results. Catch `BrokerError` for shared
infrastructure failure.

## Research workflow

Make one CLI execution carry a complete research stage:

1. **Survey:** fan out 6–12 query variants, inspect batch failures, fuse with RRF, and persist a
   bounded pool keyed by `ref`.
2. **Locate:** run focused `grep_report` patterns across the pool. Distinguish zero matches from
   per-document fetch failures.
3. **Verify:** read around useful 1-indexed match lines and retain one locatable passage for every
   required constraint.
4. **Submit:** call `sdk.output.submit` only when every material claim has verified evidence.

Use ordinary Python for regex, joins, filters, counts, ranking, dates, and coverage. Use
`extract_many` only for semantic work requiring a checked JSON object schema. Regex proves text
presence, not a relationship; verify relational claims with a relation-specific pattern or checked
extraction.

Workspace files, refs, and locators survive later `agent-run` calls in the same agent conversation;
Python variables do not. Reload the pool and evidence ledger at the start of each new program.
Use `merge_jsonl` for pool upserts and one constraint-keyed JSON object for evidence.

Print compact progress or submit a compact result. Raw hits and documents remain inside the
sandbox, so filter before printing. After a final failure, rewrite the query, backfill from another
candidate, or stop instead of repeating the same call.

If a call returns `state_lost`, its submitted program was not replayed. Treat all prior workspace
and opaque handles as gone; begin the next research stage from clean state rather than resubmitting
the same program blindly.
