---
name: search-as-code
description: Compose OpenSAC search, content, checked extraction, and trusted citation primitives as Python programs. Use for research workflows executed through the OpenSAC sandbox SDK.
---

# Search as Code

Use Python for orchestration and `opensac_sdk` only for external capabilities:

```python
from opensac_sdk import BrokerError, sdk
```

## Core primitives

- `sdk.search(query, limit=10, offset=0, domains=None) -> list[SearchHit]`
- `sdk.search.many(queries, limit_per_query=10, offset=0, concurrency=5,
  domains=None) -> list[SearchBatch]`
- `sdk.search.fuse_rrf(batches, weights=None, k=60, limit=None) -> FusionResult`
  — deterministic local computation; no broker call
- `sdk.content.grep_report(refs, pattern, context=0, max_matches_per_ref=20)`
- `sdk.content.read(refs, offset=1, limit=200, max_chars=100_000)`
- `sdk.content.get_many(refs)` and `sdk.content.snippets(query, refs, ...)`
- `sdk.llm.extract_many(items, instruction=..., schema=..., concurrency=4,
  repair_attempts=0) -> list[ExtractionResult]`
- `sdk.state.merge_jsonl`, `read_jsonl`, `write_json`, `read_json`, and `exists`
- `sdk.output.submit(output, citations=[{"ref": passage.ref,
  "locator": passage.locator}])`

Normal control flow needs only:

- search: `batch.hits/failure`, then `hit.ref/title/date/snippet/rank`;
- fusion: `result.candidates/batch_errors`, then `candidate.sources/fused_score`;
- content: `match.ref/line/text`, then `passage.ref/text/locator/failure`;
- failure: `code/message/retryable`.

Provider, retrieval, and usage details are diagnostic. Empty hits and zero matches are successes;
partial failures stay aligned, while shared infrastructure failure may raise `BrokerError`.

> Hosts: OpenSAC 0.4+ uses capability contract 3. Require it and filter methods from
> `capabilities`; keep the tool name, output limit, backend/ablation facts, provider policy, and
> answer format host-side.

## Handles, ranking, and evidence

Search creates reachability. A stable `ref` identifies the same document across queries; never
invent one. Ranks are query-local, so fuse batches with local `fuse_rrf`. It groups by ref, keeps
query/rank/score provenance in `candidate.sources`, and reports typed `batch_errors`.

Search snippets are previews. Read the content used for the answer. Eligible `grep` matches,
`read` windows, and broker-selected snippets carry an opaque, ref-bound locator. Cite that exact
passage; a ref-only citation deliberately points to the search preview. Locators are also bound to
the broker session: persist and reuse them across executions only while the same session stays live.

If evidence capacity is exhausted, the passage still has text but no locator and carries
`locator_error`. Use the text for reasoning, but never submit it as selected-passage evidence.
Explicit `{"locator": None}` is invalid.

Use `grep_report` when coverage matters: it distinguishes zero matches from per-ref fetch
failures and preserves duplicate input positions. Then `read` around a useful 1-indexed line and
verify the actual returned passage in code. `grep` is only a convenience that hides partial fetch
failures.

## Program shape

Make one execution carry a research stage:

1. Fan out independent queries, inspect failures, RRF-fuse, and persist one pool keyed by ref.
2. Run one focused `grep_report` per constraint over the whole pool.
3. Read around matches and retain one verified, locatable passage per constraint.
4. Submit only when every required constraint has evidence.

Keep raw hits/pages in variables or workspace files and print only compact progress. Use Python
for regex, joins, filters, counts, and coverage. The pool is the file: reload it each turn and use
`merge_jsonl` for upsert. Use checked `extract_many` only when a task needs fixed-shape semantic
extraction; each result has exactly one of `data/error`.

Regex verifies text presence, not a semantic relationship. For relational claims, use a relation-
specific pattern or checked `extract_many` before treating co-occurrence as evidence.

Retry, rate limiting, exact request dedupe, and optional coalescing are host policy. After a final
failure, rewrite, backfill, or stop instead of looping the same call. Never bypass the SDK through
network, subprocess, shell, credentials, or installation.

## Pattern

```python
import re

from opensac_sdk import BrokerError, sdk

pool = (
    {row.ref: dict(row) for row in sdk.state.read_jsonl("pool.jsonl")}
    if sdk.state.exists("pool.jsonl")
    else {}
)

queries = ['"exact phrase" narrowing words', "same constraint alternate wording"]
try:
    batches = sdk.search.many(queries, limit_per_query=10, concurrency=6)
except BrokerError as error:
    print(f"search failed: {error.code} retryable={error.retryable}")
    batches = []

fusion = sdk.search.fuse_rrf(batches, k=60)
for item in fusion.batch_errors:
    print(f"query failed: {item.query} code={item.failure.code}")

for candidate in fusion.candidates:
    row = pool.setdefault(
        candidate.ref,
        {
            "ref": candidate.ref,
            "title": candidate.title,
            "date": candidate.date,
            "score": 0.0,
        },
    )
    row["score"] = max(row["score"], candidate.fused_score)

sdk.state.merge_jsonl("pool.jsonl", list(pool.values()))
ordered = sorted(
    pool.values(),
    key=lambda row: (-row["score"], row["ref"]),
)
print(f"pool={len(pool)} failed_batches={len(fusion.batch_errors)}")
for row in ordered[:40]:
    print(f'{row["ref"]} {row.get("date") or "-"} {row["title"]}')

constraints = {
    "phrase": r"target phrase|alternate spelling",
    "year": r"\b(1998|1999)\b",
}
loaded = (
    sdk.state.read_json("evidence.json")
    if sdk.state.exists("evidence.json")
    else {}
)
evidence = {
    name: dict(row)
    for name, row in loaded.items()
    if name in constraints and row.get("pattern") == constraints[name]
}

support = {}
if not pool:
    print("no candidates available")
else:
    for name, pattern in constraints.items():
        if name in evidence:
            continue
        report = sdk.content.grep_report(list(pool), pattern, context=2)
        support[name] = report.matches
        for failed in report.failures:
            print(f"fetch failed: {name} ref={failed.ref} code={failed.failure.code}")

for name, matches in support.items():
    unique_matches = []
    seen_refs = set()
    for match in matches:
        if match.ref not in seen_refs:
            unique_matches.append(match)
            seen_refs.add(match.ref)
        if len(unique_matches) == 2:
            break
    for match in unique_matches:
        passage = sdk.content.read(
            [match.ref], offset=max(match.line - 10, 1), limit=40, max_chars=16_000
        )[0]
        if passage.failure is not None:
            print(f"read failed: {passage.ref} code={passage.failure.code}")
            continue
        if (
            passage.locator is not None
            and re.search(constraints[name], passage.text, re.IGNORECASE)
        ):
            evidence[name] = {
                "pattern": constraints[name],
                "ref": passage.ref,
                "text": passage.text,
                "locator": passage.locator.model_dump(mode="json"),
            }
            break
        if passage.locator_error is not None:
            print(f"locator unavailable: {passage.locator_error.code}")

if evidence:
    sdk.state.write_json("evidence.json", evidence)
missing = [name for name in constraints if name not in evidence]
print("unverified:", missing or "none")

if evidence and not missing:
    sdk.output.submit(
        {
            "evidence": [
                {"constraint": name, "ref": row["ref"], "text": row["text"]}
                for name, row in evidence.items()
            ]
        },
        citations=[
            {"ref": row["ref"], "locator": row["locator"]}
            for row in evidence.values()
        ],
    )
```
