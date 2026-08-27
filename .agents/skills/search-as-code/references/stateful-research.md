# Lightweight state for multi-call research

Use state when later programs can save external work by reusing search candidates or inspected
content. Multiple `sac_run` calls alone do not require state; read [patterns.md](patterns.md) for the
default stateless pipeline.

## Small data model

Three artifacts are usually enough:

| Artifact | Reusable data |
| --- | --- |
| `meta.json` | Task, attempted queries, bounded failure or coverage summaries |
| `pool.jsonl` | Normalized search candidates, deduplicated by `source` |
| `content.jsonl` | Inspected passages or read windows with source, coordinates, and text |

These files are a data cache, not a workflow state machine. Do not create per-stage logs, duplicate
raw reports, or a final ledger unless the caller needs one. Use ordinary Python variables for values
consumed inside the same program.

Keep one cumulative file for each role. Update `pool.jsonl` by `source` and `content.jsonl` by a
stable source-window key; do not create `pool_round2.jsonl` or `content_stage3.jsonl`. The program
that fetches content should print bounded target excerpts plus explicit no-match, blocked, and typed
failure summaries before it ends. A later state-only program is useful when asking a new semantic
question of cached text, not simply to display a document the fetching program could have surfaced.

Each later program should load the rows it needs, filter already attempted queries or fetched
sources, extend the cache, and print the bounded decision surface needed by the agent. Bound or prune
the files according to the task and workspace budget.

## One composed cache-extending program

This example searches only new queries, updates the candidate pool, inspects previously unfetched
sources, and stores focused windows. Adapt its inputs and bounds; they are not a required strategy.

```python
import hashlib
import json

from opensac_sdk import sdk

task = "Identify the target and verify the requested relation."
research_id = hashlib.sha256(task.encode()).hexdigest()[:12]
root = f"runs/{research_id}"
meta_path = f"{root}/meta.json"
pool_path = f"{root}/pool.jsonl"
content_path = f"{root}/content.jsonl"

meta = dict(sdk.state.read_json(meta_path)) if sdk.state.exists(meta_path) else {
    "task": task,
    "queries": [],
    "failures": [],
}
pool = [dict(row) for row in sdk.state.read_jsonl(pool_path)] if sdk.state.exists(pool_path) else []
content = (
    [dict(row) for row in sdk.state.read_jsonl(content_path)]
    if sdk.state.exists(content_path)
    else []
)

planned_queries = ['"exact phrase" entity', "rare clue alternate wording"]
tried = set(meta["queries"])
queries = [query for query in dict.fromkeys(planned_queries) if query not in tried]
pool_by_source = {row["source"]: row for row in pool}

if queries:
    # Save attempted queries before an expensive call only when avoiding blind replay matters.
    meta["queries"] = sorted(tried | set(queries))
    sdk.state.write_json(meta_path, meta)
    search_report = sdk.search.many(queries, limit_per_query=10, concurrency=4)
    fused = sdk.search.fuse_rrf(search_report, k=60, limit=50)
    for row in fused:
        pool_by_source[row.source] = {
            "source": row.source,
            "title": row.title,
            "domain": row.domain,
            "date": row.date,
            "snippet": (row.snippet or "")[:500],
            "rank": row.fused_rank,
            "score": row.fused_score,
            "provenance": row.provenance,
        }
    meta["failures"].extend(
        {"stage": "search", "query": row.query, "code": row.code}
        for row in search_report.failures
    )

pool = sorted(pool_by_source.values(), key=lambda row: row.get("rank", 1_000_000))[:300]
sdk.state.write_jsonl(pool_path, pool)

inspected_sources = {row["source"] for row in content}
sources = [row["source"] for row in pool if row["source"] not in inspected_sources][:24]
if sources:
    passage_report = sdk.content.passages(task, sources, limit=10, max_per_source=2)
    windows = []
    seen = set()
    for passage in passage_report.passages:
        key = (passage.source, passage.coordinates["start_line"])
        if key in seen:
            continue
        seen.add(key)
        windows.append(
            {
                "source": passage.source,
                "offset": max(passage.coordinates["start_line"] - 8, 1),
                "limit": 50,
                "max_chars": 16_000,
                "passage_coordinates": dict(passage.coordinates),
            }
        )
        if len(windows) >= 6:
            break

    read_report = sdk.content.read_many(
        [
            {key: row[key] for key in ("source", "offset", "limit", "max_chars")}
            for row in windows
        ]
    ) if windows else None
    window_by_index = {index: row for index, row in enumerate(windows)}
    content_by_key = {row["key"]: row for row in content}
    for row in (read_report.results if read_report else []):
        window = window_by_index[row.input_index]
        start = row.metadata.get("start_line")
        end = row.metadata.get("end_line")
        key = f"{row.source}#L{start}-{end}"
        content_by_key[key] = {
            "key": key,
            "source": row.source,
            "title": row.title,
            "text": row.text,
            "coordinates": {
                "start_line": start,
                "end_line": end,
                "passage": window["passage_coordinates"],
            },
        }
    meta["failures"].extend(
        {"stage": "passages", "source": row.source, "code": row.code}
        for row in passage_report.failures
    )
    meta["failures"].extend(
        {"stage": "read", "source": row.source, "code": row.code}
        for row in (read_report.failures if read_report else [])
    )
    content = list(content_by_key.values())[-300:]
    sdk.state.write_jsonl(content_path, content)

meta["failures"] = meta["failures"][-100:]
sdk.state.write_json(meta_path, meta)

print(
    f"CACHE research={research_id} pool={len(pool)} content={len(content)} "
    f"new_queries={len(queries)} failures={len(meta['failures'])}"
)
for row in content[-4:]:
    excerpt = " ".join(row["text"].split())[:500]
    print(f"EVIDENCE source={row['source']!r} excerpt={excerpt!r}")
print("NEXT: judge these rows, answer if complete, or extend unresolved requirements")
```

The example filters sources already represented in `content.jsonl`; a task may deliberately revisit
one source for a different window or requirement. Use a stable composite `key` for those windows.

After an adapter failure with unknown outcome, inspect the existing cache and session usage before
choosing new work. After explicit `state_lost`, rebuild state and re-admit local IDs; public URLs
remain reusable. Use `sdk.output.submit(...)` only when structured runtime output is requested.
