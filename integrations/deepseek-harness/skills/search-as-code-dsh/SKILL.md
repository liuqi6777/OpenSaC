# Search as Code with dsh

Use the native `sac_run` tool for programmable, evidence-grounded research. Pass a complete Python
program in its `code` argument. Do not invoke `opensac`, shell commands, REST endpoints, MCP
context-binding tools, or session-management APIs yourself; the dsh plugin owns those boundaries.

```python
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
        print(passage.ref, passage.locator, passage.text[:500])
```

Core primitives:

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

Treat search snippets as previews. Read every passage used as evidence and preserve the returned
opaque `ref` and `locator` exactly; never invent, edit, or reconstruct them. Do not cite text whose
locator is missing or reports `evidence_capacity_exhausted`. Inspect aligned `failure` values and
catch `BrokerError` for shared infrastructure failures.

For nontrivial research, work in stages: fan out query variants and fuse them, locate candidate
passages, verify each required constraint, then submit only supported claims. Use ordinary Python
for regex, joins, filters, ranking, dates, and coverage; reserve `extract_many` for semantic work
that needs a checked JSON schema.

Workspace files, refs, and locators survive later `sac_run` calls by the same dsh agent; Python
variables do not. Persist only bounded pools and evidence ledgers. If a call returns `state_lost`,
the program was not replayed and the next call starts from clean state—do not blindly resubmit a
possibly indeterminate execution.
