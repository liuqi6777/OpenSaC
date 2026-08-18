---
name: search-as-code
description: Compose OpenSAC search, content, checked extraction, and trusted citation primitives as Python programs. Use for research workflows when the sac_run MCP tool is available.
---

# Search as Code

Invoke the model-visible MCP tool `sac_run(code)` and pass only the Python research program.
Never create, resume, or delete REST sessions yourself. `bind_context` is host-internal: never
call it; the MCP host binds the current agent conversation automatically.

Use Python for orchestration and `opensac_sdk` only for external capabilities:

```python
from opensac_sdk import BrokerError, sdk
```

## Core primitives

- sdk.search(query, limit=10, offset=0) -> list[SearchHit]
- sdk.search.many(queries, limit_per_query=10, offset=0, concurrency=5) -> list[SearchBatch]
- sdk.content.get_many(refs) -> list[ContentSnippet]   # whole text
- sdk.content.snippets(query, refs, max_tokens=4000, max_tokens_per_page=1000)
- sdk.content.grep(refs, pattern, context=0, max_matches_per_ref=20) -> list[ContentMatch]
- sdk.content.grep_report(refs, pattern, context=0, max_matches_per_ref=20) -> ContentGrepReport
- sdk.content.read(refs, offset=1, limit=200, max_chars=100000) -> list[ContentSnippet]
- sdk.citations.resolve(refs) -> title / url / docid / evidence
- sdk.session.usage() -> strategy counts and remaining budget
- sdk.llm.complete(prompt, system=None, temperature=0.2) -> str
- sdk.llm.complete_many(prompts, concurrency=4) -> list[str]
- sdk.llm.extract_many(items, instruction=..., schema=..., concurrency=4, repair_attempts=0) -> list[ExtractionResult]
- sdk.search.fuse_rrf(batches, weights=None, k=60, limit=None) -> FusionResult  # in sandbox, no RPC
- sdk.state.merge_jsonl(path, rows, key="ref") -> pool size   # read, upsert, write
- sdk.state.write_json / write_jsonl / append_jsonl / read_json / read_jsonl / exists / list
- sdk.output.submit(output, citations=[{"ref": passage.ref, "locator": passage.locator}])

Exact fields by type:
- SearchBatch: hits, failure
- SearchHit: ref, title, date, snippet, rank, backend
- SearchCandidate: ref, title, date, snippet, rank, sources, fused_score
- ContentMatch: ref, line, text
- ContentSnippet: ref, title, date, text, metadata, locator, locator_error, failure
- ContentGrepReport: matches, failures, input_count
- GrepFailure: ref, input_index, failure
- Failure: code, message, retryable, attempts
Join by ref. Partial failures stay aligned; BrokerError is infrastructure. Empty hits and zero
matches succeed. Content accepts a returned ref, docid or URL; stable refs dedup the pool.

## Reading documents

- grep finds 1-indexed matching lines across documents; read uses that line as its offset.
- get_many returns whole text; snippets returns a broker-selected window.

Documents are cached. Reads return aligned rows; grep hides partial failures while grep_report
exposes them.

Non-empty passages up to 16,000 chars carry a locator. Cite used passage; ref-only means search
preview. A full evidence registry returns locator=None and
locator_error.code="evidence_capacity_exhausted"; report but never cite it.
Explicit locator:null is rejected. Persist broker id/ref/kind losslessly across turns only
inside this rollout's live session; never invent, edit or reconstruct one.

There is one search configured by the OpenSAC service. offset is depth into its ranking,
not merely paging: a document is readable only if a search returned it, so limit is at
once how far you can see and how far you may reach.

Dunder attributes are rejected by the sandbox apart from `__name__` and `__doc__`, so
report failures with `type(exc).__name__` and do not introspect via `__class__`.

## How to write a program

One sac_run should carry a whole research stage -- fan out, filter, read, extract, report.
A single-action program wastes a round trip.

Do deterministic work in plain Python: deduplication, regex, joins, set arithmetic,
counting, ranking, coverage checks, date filtering. None of it needs a capability.

Reach for sdk.llm.extract_many when you need semantics in a fixed shape, and
sdk.llm.complete only for planning steps with no schema, such as summarizing current
coverage and proposing follow-up queries. Validate anything an LLM returns with code
before acting on it. extract_many returns one ExtractionResult per input, in order;
exactly one of result.data and result.error is set. Inspect error.code/message/retryable.
repair_attempts=1 retries invalid JSON or a schema mismatch once; default zero. The JSON
Schema root must be an object -- Python types such as str are not schemas.

Retry, rate limits, dedupe and optional in-flight coalescing are host policy, not SDK arguments.
failure.attempts includes host attempts; after final failure, rewrite, backfill or stop.
Only what you print or submit comes back to you -- raw hits and page text never enter this
conversation. That channel holds about 32000 characters per call across stdout,
stderr and the submitted output; past that the middle is elided and both ends kept.

Use three stages: **survey** -- fan out 6-12 queries across phrasings, entities and
constraints, then merge, print compactly and save; **locate** -- grep the whole pool for
distinguishing names, dates, numbers and phrases, then read around useful matches;
**verify** -- check every constraint separately, in code where mechanical, and retain the
passage that settles it. They need not be separate turns.

RRF orders a survey; it is not a cutoff and agreement can be a popularity prior. Preserve the
best few hits from every query, especially exact or rare-clue queries. Once a result identifies
the target entity or source, stop broad fan-out; use exact follow-ups and grep/read the relation.

Use hit snippets to triage candidates, reject mismatches and form exact follow-up queries; persist
a bounded preview with the pool. A snippet is not final evidence. Grep before fetching, open only
a few distinct refs, and save opened refs. Exclude those refs from later content calls unless
reading a different line window. On reread>0, fix selection; repeating the same broad search or
get_many batch is not progress.

Between turns the workspace and handles survive; variables do not. merge_jsonl upserts the
pool by ref and treats an absent file as empty. read_jsonl rows support row.ref and row["ref"].
Keep evidence in one constraint-keyed JSON object; serialize the broker locator losslessly with
locator.model_dump(mode="json"). sdk.session.usage() reports strategy spend and remaining budget.
Regex proves text presence, not a relationship: use a relation-specific pattern or checked
extract_many.

## What goes wrong

- **Answering from search snippets.** A snippet is a retrieval preview chosen by the
  index, not evidence. If no content call was made, the answer is a guess.
- **Reading the printed list instead of the pool.** What gets printed is a window onto
  the pool, not the pool. Filter and read by variable; a handle you retyped is both a
  chance to mistype it and a sign you narrowed to what happened to be on screen.
- **Stopping at the first match.** Keep reading until every required constraint has a passage.
- **Dumping.** Printing raw snippets or whole pages fills the observation budget with
  material the program already holds. Filter before printing, not after.

## Pattern

```python
import re

from opensac_sdk import BrokerError, sdk

# The pool is the file: reload, add, merge back; pool2.jsonl is never needed.
pool = {r.ref: dict(r) for r in sdk.state.read_jsonl("pool.jsonl")} if sdk.state.exists("pool.jsonl") else {}

# Single-quoted, so a phrase query carries its quotes without a backslash.
queries = ['"exact phrase" narrowing words', 'the same constraint, said differently']
try:
    batches = sdk.search.many(queries, limit_per_query=10, concurrency=6)
except BrokerError as error:
    print(
        f"search failed: code={error.code} retryable={error.retryable} "
        f"attempts={error.attempts}"
    )
    batches = []
fusion = sdk.search.fuse_rrf(batches, k=60)
for item in fusion.batch_errors:
    print(f"query failed: {item.query} code={item.failure.code}")
for candidate in fusion.candidates:
    row = pool.setdefault(candidate.ref, {
        "ref": candidate.ref, "title": candidate.title, "date": candidate.date,
        "snippet": "", "score": 0.0,
    })
    if candidate.snippet:
        row["snippet"] = candidate.snippet[:400].replace("\n", " ")
    row["score"] = max(row["score"], candidate.fused_score)
sdk.state.merge_jsonl("pool.jsonl", list(pool.values()))

ordered = sorted(pool.values(), key=lambda r: (-r["score"], r["ref"]))
# RRF top results plus each query's own leaders: consensus must not hide a needle.
shortlist_refs = {r["ref"] for r in ordered[:20]}
for batch in batches:
    if batch.failure is None:
        shortlist_refs.update(hit.ref for hit in batch.hits[:2])
for r in ordered:
    if r["ref"] not in shortlist_refs:
        continue
    print(
        f'score={r["score"]:.4f} {r["ref"]} {r.get("date") or "-"} {r["title"]}'
        f' :: {r.get("snippet", "")[:240]}'
    )

constraints = {"phrase": r"(target phrase|other spelling)", "year": r"\b(1998|1999)\b"}
loaded = sdk.state.read_json("evidence.json") if sdk.state.exists("evidence.json") else {}
evidence = {
    name: dict(row)
    for name, row in loaded.items()
    if name in constraints and row.get("pattern") == constraints[name]
}

# A per-constraint, distinct-document cap keeps one regex from starving the others.
if not pool:
    print("no candidates available")
else:
    for name, pattern in constraints.items():
        if name in evidence:
            print(f"{name}: verified in ledger")
            continue
        report = sdk.content.grep_report(list(pool), pattern, context=2)
        for failed in report.failures:
            print(
                f"fetch failed: {name} input={failed.input_index} ref={failed.ref} "
                f"code={failed.failure.code} attempts={failed.failure.attempts}"
            )
        print(f"{name}: {len(report.matches)} matching lines in {len(pool)} docs")
        unique_matches, seen_refs = [], set()
        for match in report.matches:
            if match.ref not in seen_refs:
                unique_matches.append(match)
                seen_refs.add(match.ref)
            if len(unique_matches) == 3:
                break
        for match in unique_matches:
            passage = sdk.content.read(
                [match.ref], offset=max(match.line - 10, 1), limit=40, max_chars=16_000
            )[0]
            if passage.failure is not None:
                print(f"read failed: {passage.ref} code={passage.failure.code}")
                continue
            if passage.locator is None or not re.search(pattern, passage.text, re.IGNORECASE):
                if passage.locator_error is not None:
                    print(f"locator unavailable: {passage.locator_error.code}")
                continue
            evidence[name] = {
                "pattern": pattern,
                "ref": passage.ref,
                "text": passage.text,
                "locator": passage.locator.model_dump(mode="json"),
            }
            print(f"{name}: verified {passage.ref}")
            break
if evidence:
    sdk.state.write_json("evidence.json", evidence)

missing = [name for name in constraints if name not in evidence]
print("unsupported:", missing or "none")

# An unsupported constraint is the next turn's work; do not submit around it.
if evidence and not missing:
    sdk.output.submit(
        {"evidence": [
            {"constraint": name, "ref": row["ref"], "text": row["text"]}
            for name, row in evidence.items()
        ]},
        citations=[
            {"ref": row["ref"], "locator": row["locator"]}
            for row in evidence.values()
        ],
    )
```
