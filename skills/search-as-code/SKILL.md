---
name: search-as-code
description: Compose OpenSAC search primitives as Python programs.
---

# Search as Code

Use Python for orchestration and `opensac_sdk` for every external capability:

```python
from opensac_sdk import sdk
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

`SearchHit` has `ref`, `backend`, `title`, `url`, `docid`, `domain`, `date`,
`snippet`, `score`, `rank`, and optional effective `retrieval` metadata. `SearchBatch`
has `query`, `hits`, `error`, and `request`; check `error` before treating empty hits as
“nothing found.” Result objects support attribute and mapping reads, and state methods accept
them directly. A row returned by `read_jsonl` likewise supports `row.ref` and `row["ref"]`.

> Hosts: filter broker methods from session `capabilities`; programs cannot read the
> manifest. Keep the tool name, output limit, backend/ablation facts, and answer format
> host-side.

## Handles, provenance, and evidence

A `ref` is stable for the document behind it, so the same page found by several queries
deduplicates by ref. Content methods also accept a `docid` or URL from a hit already returned
in this session. Handles cannot be invented: search is the only door into the corpus.

Search is backend-neutral; the deployment chooses its corpus and `hit.backend` records it.
Unsupported parameters are refused. `offset` is ranking depth, and only returned documents
become readable.

Every query has its own rank 1, so raw `rank` does not order a cross-query pool. Pass
`SearchBatch` objects to `fuse_rrf`. It groups by ref, preserves every source query/rank/score
in `candidate.sources`, reports failed batches in `batch_errors`, and spends no capability
call. Query weights, `k`, and the final limit remain choices made by the program.

Non-empty `snippets`, `grep` matches, and reasonably sized `read` windows carry a
broker-issued `locator`. Cite the passage actually used with
`{"ref": passage.ref, "locator": passage.locator}`. A citation with only `ref` deliberately
resolves the search preview. A locator is opaque and bound to its ref; never construct, edit,
or attach it to a docid, URL, or different ref.

## Reading documents

- `get_many` returns whole pages.
- `snippets` returns one broker-scored window per page.
- `grep` locates matching lines across many documents; `line` is 1-indexed.
- `read` returns a line window. Its metadata includes `start_line`, `end_line`,
  `total_lines`, and `next_offset`; a `ContentMatch.line` plugs into `read(offset=...)`.

Documents are cached per session, so grep then read beats dumping pages. `get_many`,
`snippets`, and `read` return one ordered row per handle; a page failure has empty text and
`metadata["fetch_error"]`. `grep` returns zero or more matches per document. If every fetch
fails, the call raises.

## How to write a program

The sandbox is a computer, not a slower function call. One execution should carry a whole
research stage — fan out, rank, locate, read, extract, report. Only compact material the
program prints or submits enters the control-model conversation; raw hits and pages should
stay in variables or the workspace.

Three stages usually finish a question:

1. **Survey.** Fan out 6–12 independent queries over different phrasings, entities, and
   constraints. Check batch failures, fuse with RRF, and persist one pool keyed by ref. Print
   only a small ranked window with ref, date, and title.
2. **Locate.** Run one focused grep per constraint over every ref in the persistent pool, not
   merely the printed window. Read around matches in every distinct document worth following.
3. **Verify.** Track which constraints have actual passages behind them and check mechanical
   constraints in code. An unsupported constraint is the next execution's work, not a detail
   to submit around. Different constraints often require different supporting documents.

Use Python for regex, joins, filtering, counting, set arithmetic, and coverage. For fixed-shape
semantics, `llm.extract_many` takes an object-root JSON Schema and returns one ordered
`ExtractionResult` per input with exactly one of `data` or `error`; inspect
`error.code/message/retryable`. `repair_attempts=1` allows one format/schema repair and defaults
to zero. Schema types are JSON values such as `{"type": "string"}`, not Python `str`. Reserve
`llm.complete` for results without a fixed schema and validate them before use.

The pool is the file, not the variable. Each execution is a new process: the workspace and
handles survive, Python names do not. `merge_jsonl` treats an absent file as empty and upserts
by ref, so one pool survives even when a later program forgets to reload before writing. What
gets printed is a window onto the pool, not the pool: pass refs from the mapping to content
calls and never retype handles from output. Use `append_jsonl` only for true event logs where
duplicates are intentional.

Defaults worth starting from:

- `limit_per_query=10`, then `offset=10` on a query that looked promising.
- `concurrency=6` for fan-out.
- One whole-pool `grep` per constraint with `context=2`.
- `read(offset=max(match.line - 10, 1), limit=40, max_chars=16_000)` around a promising
  match when it must carry a locator.

## What goes wrong

- **Answering from search snippets.** They are index-selected previews, not evidence. If no
  content call was made, the answer is a guess.
- **Searching again instead of reading.** When coverage stops increasing, inspect the pool
  already held before adding more queries.
- **Sorting merged results by raw rank.** Use RRF and do not choose what to read from an
  arbitrary printed slice.
- **Stopping at the first fitting document.** Keep reading while any constraint lacks a
  passage.
- **Reaching for last execution's variable.** Reload the workspace file. Avoid fragmented
  `pool2.jsonl`, `pool3.jsonl`, and similar files.
- **Dumping.** Filter before printing. Raw hits, snippets, and pages consume the observation
  channel with material the program already holds.

Wrap fragile stages and report `type(exc).__name__` plus the query, ref, or stage. The sandbox
rejects `__class__`/`__dict__` introspection. Do not retry unknown handles or bypass the SDK
with HTTP, sockets, subprocesses, shell, credentials, environment inspection, or installation.

## Pattern

```python
import re

from opensac_sdk import sdk

# The pool is the file, not the variable: reload it before adding this turn's work.
pool = (
    {row.ref: dict(row) for row in sdk.state.read_jsonl("pool.jsonl")}
    if sdk.state.exists("pool.jsonl")
    else {}
)

# Use new follow-up queries on later turns; repeats would be weighted again.
# Single-quoted strings carry phrase-query quotes without escaping them.
queries = ['"exact phrase" narrowing words', 'same constraint alternate wording']
batches = sdk.search.many(queries, limit_per_query=10, concurrency=6)
fusion = sdk.search.fuse_rrf(batches, k=60)
for failure in fusion.batch_errors:
    print(f"query failed: {failure.query}: {failure.error}")

# Accumulate local RRF scores so ranking and the document pool both survive turns.
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
    support[name] = sdk.content.grep(list(pool), pattern, context=2)
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
