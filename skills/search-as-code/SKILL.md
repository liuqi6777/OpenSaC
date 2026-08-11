---
name: search-as-code
description: Compose OpenSAC search, content, checked extraction, and trusted citation primitives as Python programs. Use for research workflows executed through the OpenSAC sandbox SDK.
---

# Search as Code

Use Python for orchestration and `opensac_sdk` for external capabilities:

```python
from opensac_sdk import BrokerError, sdk
```

## Primitives

- `sdk.search(query, limit=10, offset=0, domains=None) -> list[SearchHit]`
- `sdk.search.many(queries, limit_per_query=10, offset=0, concurrency=5,
  domains=None) -> list[SearchBatch]`
- `sdk.search.fuse_rrf(batches, weights=None, k=60, limit=None) -> FusionResult`
  — deterministic local computation; no broker call
- `sdk.content.get_many(refs)` — whole text
- `sdk.content.snippets(query, refs, max_tokens=4000, max_tokens_per_page=1000)`
- `sdk.content.grep(refs, pattern, context=0, max_matches_per_ref=20)`
- `sdk.content.grep_report(refs, pattern, context=0, max_matches_per_ref=20) ->
  ContentGrepReport`
- `sdk.content.read(refs, offset=1, limit=200, max_chars=100_000)`
- `sdk.citations.resolve(refs)`
- `sdk.llm.complete(prompt, system=None, temperature=0.2, max_tokens=None)`
- `sdk.llm.complete_many(prompts, concurrency=4, ...)`
- `sdk.llm.extract_many(items, instruction=..., schema=..., concurrency=4,
  repair_attempts=0) -> list[ExtractionResult]`
- `sdk.state.merge_jsonl(path, rows, key="ref") -> int` — read, upsert, write
- `sdk.state.write_json/write_jsonl/append_jsonl/read_json/read_jsonl/exists/list`
- `sdk.session.usage()`
- `sdk.output.submit(output, citations=[{"ref": passage.ref,
  "locator": passage.locator}])`

`SearchHit` exposes `ref`, provenance, metadata, score, rank, and optional effective
`retrieval`. `SearchBatch` exposes `query`, `hits`, `failure`, deprecated `error`, and
`request`. Before treating empty hits as no result, inspect `failure.code/message/retryable`,
`attempts`, and optional provider status/Retry-After. Partial failures stay aligned; shared
infrastructure failure can raise `BrokerError`. Empty hits and zero matches are successes.
`BrokerError` exposes `code`, `retryable`, `attempts`, provider status, and Retry-After directly.
Models and `read_jsonl` rows support attribute and mapping reads.

> Hosts: OpenSAC 0.3+ uses capability contract 2; require that contract and filter methods from
> `capabilities`; programs cannot read the manifest. Keep the tool name, output limit,
> backend/ablation facts, provider policy, and answer format host-side.

## Handles, provenance, and evidence

Search creates reachability. A stable `ref` deduplicates the same document across queries;
content also accepts a returned docid or URL. Never invent handles. The host chooses the
backend, `hit.backend` records it, unsupported parameters fail, and `offset` means ranking
depth.

Ranks are query-local. Fuse `SearchBatch` objects with local `fuse_rrf`: it groups by ref,
keeps query/rank/score in `candidate.sources`, and reports typed `batch_errors` without an RPC.
Choose weights, `k`, and cutoff in the program.

Eligible `snippets`, `grep` matches, and `read` windows carry a broker locator. Cite the used
passage with `{"ref": passage.ref, "locator": passage.locator}`; ref-only deliberately cites
the search preview. Locators are opaque and ref-bound—never invent or edit one. Losslessly
persisting the broker-returned `id/ref/kind` fields and submitting that mapping later is allowed.

The evidence registry is bounded. On capacity exhaustion, use the returned text for reasoning
but do not cite it as selected evidence: `locator=None` and
`locator_error.code == "evidence_capacity_exhausted"`. Explicit `{"locator": None}` is
rejected; never silently fall back to preview.

## Reading documents

- Use `get_many` for whole pages and `snippets` for broker-selected windows.
- Use `grep_report` to locate 1-indexed lines while distinguishing zero matches, duplicate
  inputs, and typed per-input failures. Legacy `grep` hides partial fetch failures.
- Use `read(offset=match.line, ...)` for a line window; metadata carries start/end/total and
  `next_offset`.

Documents cache per session. Ordered content failure rows have empty text and typed `failure`;
`metadata["fetch_error"]` is only a compatibility mirror. All-fetch failure may raise.

## How to write a program

Make one execution carry a research stage—fan out, rank, locate, read, extract, report. Keep
raw hits/pages in variables or the workspace; only print or submit compact results.

Three stages usually finish a question:

1. **Survey:** fan out 6–12 independent queries, check failures, RRF-fuse, and persist one pool
   keyed by ref; print only a ranked window.
2. **Locate:** run one focused `grep_report` per constraint over the whole pool, then read around
   useful matches.
3. **Verify:** retain a passage per constraint and check mechanical constraints in code. Keep
   working when anything is unsupported; constraints may require different documents.

Use Python for regex, joins, filtering, counts, and coverage. For fixed-shape semantics, call
`llm.extract_many` with an object-root JSON Schema; each ordered result has exactly one of
`data`/`error`. Inspect typed errors. `repair_attempts=1` permits one format/schema repair
(default 0). Use JSON types, not Python `str`. Validate free-form `llm.complete` output.

Treat retry, rate limiting, dedupe, and optional coalescing as host policy, not SDK knobs.
`failure.attempts` includes host attempts; after final failure, rewrite, backfill, or stop
instead of looping the same call.

The pool is the file: processes lose variables, while workspace and handles survive.
`merge_jsonl` upserts by ref and treats a missing file as empty. Reload before each turn, pass
refs from the mapping, and treat printed rows as a window—not the pool. Use `append_jsonl` only
for intentional duplicate events.

Defaults worth starting from:

- Start with `limit_per_query=10`, `concurrency=6`; use `offset=10` for a promising query.
- Run one whole-pool `grep_report(context=2)` per constraint.
- Read around a match with `offset=max(match.line - 10, 1)`, `limit=40`,
  `max_chars=16_000` when it needs a locator.

## What goes wrong

- Search snippets are previews, not evidence; read content before answering.
- Read the held pool when coverage stalls; do not keep searching or sort cross-query hits by
  raw rank.
- Keep reading until every constraint has a passage. Reload the one pool file each turn.
- Filter before printing; never dump hits/pages.
- Never cite locator-less text as selected evidence or manufacture a locator.

Catch `BrokerError` around fragile stages and report code/retryable/attempts plus query/ref/stage.
Do not retry unknown handles or bypass the SDK through network, subprocess, shell, credentials,
or installation.

## Pattern

```python
import re

from opensac_sdk import BrokerError, sdk

# The pool is the file, not the variable: reload it before adding this turn's work.
pool = (
    {row.ref: dict(row) for row in sdk.state.read_jsonl("pool.jsonl")}
    if sdk.state.exists("pool.jsonl")
    else {}
)

queries = ['"exact phrase" narrowing words', 'same constraint alternate wording']
try:
    batches = sdk.search.many(queries, limit_per_query=10, concurrency=6)
except BrokerError as error:
    print(f"search failed: {error.code} retryable={error.retryable} attempts={error.attempts}")
    batches = []
fusion = sdk.search.fuse_rrf(batches, k=60)
for failure in fusion.batch_errors:
    detail = failure.failure
    print(
        f"query failed: {failure.query}: {failure.error}"
        + (f" code={detail.code} attempts={detail.attempts}" if detail else "")
    )

for candidate in fusion.candidates:
    row = pool.setdefault(
        candidate.ref,
        {
            "ref": candidate.ref,
            "title": candidate.title,
            "date": candidate.date,
            "rank": candidate.rank,
            "rrf": 0.0,
            "queries": 0,
        },
    )
    row["rrf"] += candidate.fused_score
    row["queries"] += len(candidate.sources)
    row["rank"] = min(row["rank"], candidate.rank)

pool_size = sdk.state.merge_jsonl("pool.jsonl", list(pool.values()))
ordered = sorted(
    pool.values(),
    key=lambda row: (-row["rrf"], -row["queries"], row["rank"], row["ref"]),
)
print(f"pool={pool_size} failed_batches={len(fusion.batch_errors)}")
for row in ordered[:40]:
    print(
        f'{row["queries"]}q r{row["rank"]} {row["ref"]} '
        f'{row.get("date") or "-"} {row["title"]}'
    )

# One focused grep per constraint, each over the whole persistent pool.
constraints = {
    "phrase": r"target phrase|alternate spelling",
    "year": r"\b(1998|1999)\b",
}
support = {}
for name, pattern in constraints.items():
    report = sdk.content.grep_report(list(pool), pattern, context=2)
    support[name] = report.matches
    for failed in report.failures:
        print(
            f"fetch failed: {name} input={failed.input_index} ref={failed.ref} "
            f"code={failed.failure.code} attempts={failed.failure.attempts}"
        )
    print(f"{name}: {len({m.ref for m in support[name]})} of {len(pool)} docs")
missing = [name for name, matches in support.items() if not matches]
print("unsupported:", missing or "none")

# Evidence is a cross-turn ledger, keyed by the constraint and exact match coordinate.
ledger = (
    {row.evidence_key: dict(row) for row in sdk.state.read_jsonl("evidence.jsonl")}
    if sdk.state.exists("evidence.jsonl")
    else {}
)
verified = {
    row["constraint"]
    for row in ledger.values()
    if row.get("pattern") == constraints.get(row.get("constraint"))
}
for name, matches in support.items():
    if name in verified:
        continue
    for match in matches[:2]:
        passage = sdk.content.read(
            [match.ref], offset=max(match.line - 10, 1), limit=40, max_chars=16_000
        )[0]
        if passage.failure is not None:
            print(f"read failed: {passage.ref} code={passage.failure.code}")
            continue
        if (
            passage.text
            and passage.locator is not None
            and re.search(constraints[name], passage.text, re.IGNORECASE)
        ):
            key = f"{name}|{constraints[name]}|{passage.ref}|{match.line}"
            ledger[key] = {
                "evidence_key": key,
                "constraint": name,
                "pattern": constraints[name],
                "ref": passage.ref,
                "line": match.line,
                "title": passage.title,
                "text": passage.text,
                "locator": {
                    "id": passage.locator.id,
                    "ref": passage.locator.ref,
                    "kind": passage.locator.kind,
                },
            }
            verified.add(name)
            print(f"--- {passage.ref} {passage.title}\n{passage.text}")
            break
        if passage.locator_error is not None:
            print(f"locator unavailable: {passage.locator_error.code}")

current_evidence = [
    row
    for row in ledger.values()
    if row.get("pattern") == constraints.get(row.get("constraint"))
]
if ledger:
    sdk.state.merge_jsonl(
        "evidence.jsonl", list(ledger.values()), key="evidence_key"
    )
unverified = [name for name in constraints if name not in verified]
print("unverified:", unverified or "none")

if current_evidence and not unverified:
    sdk.output.submit(
        {
            "evidence": [
                {
                    "constraint": row["constraint"],
                    "ref": row["ref"],
                    "text": row["text"],
                }
                for row in current_evidence
            ]
        },
        citations=[
            {"ref": row["ref"], "locator": row["locator"]}
            for row in current_evidence
        ],
    )
```
