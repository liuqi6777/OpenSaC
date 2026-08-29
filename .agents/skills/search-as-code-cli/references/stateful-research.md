# Lightweight state for multi-call research

Use state when later programs can save external work by reusing search candidates or inspected
content. Multiple `agent-run` calls alone do not require state; read [patterns.md](patterns.md) for the
default stateless pipeline.

## Small data model

Three artifacts are usually enough:

| Artifact | Reusable data |
| --- | --- |
| `meta.json` | Task, attempted queries, bounded failure or coverage summaries |
| `pool.jsonl` | Normalized search candidates, deduplicated by `source` |
| `content.jsonl` | Bounded local excerpts or semantic passages with source and coordinates |

These files are a data cache, not a workflow state machine. Do not create per-stage logs, duplicate
raw reports, or a final ledger unless the caller needs one. Use ordinary Python variables for values
consumed inside the same program.

Keep one cumulative file for each role. Update `pool.jsonl` by `source` and `content.jsonl` by a
stable source-window key; do not create `pool_round2.jsonl` or `content_stage3.jsonl`. The program
that fetches content should print bounded target excerpts plus explicit no-match, blocked, and
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
from opensac_sdk import BrokerError, sdk

task = "Identify the target and verify the requested relation."
research_id = hashlib.sha256(task.encode()).hexdigest()[:12]
root = f"runs/{research_id}"
meta_path = f"{root}/meta.json"
pool_path = f"{root}/pool.jsonl"
content_path = f"{root}/content.jsonl"

meta = (
    dict(sdk.state.read_json(meta_path))
    if sdk.state.exists(meta_path)
    else {
        "task": task,
        "queries": [],
        "fetched_sources": [],
        "failures": [],
    }
)
meta.setdefault("fetched_sources", [])
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
new_candidates = []

if queries:
    # Save attempted queries before an expensive call only when avoiding blind replay matters.
    meta["queries"] = sorted(tried | set(queries))
    sdk.state.write_json(meta_path, meta)
    search_outcomes = sdk.search.many(queries, limit=10, concurrency=4)
    fused = sdk.search.fuse_rrf(search_outcomes, k=60, limit=50)
    for row in fused:
        candidate = {
            "source": row.source,
            "title": row.title,
            "domain": row.domain,
            "date": row.date,
            "snippet": (row.snippet or "")[:500],
            "rank": row.fused_rank,
            "score": row.fused_score,
            "provenance": row.provenance,
        }
        pool_by_source[row.source] = candidate
        new_candidates.append(candidate)
    meta["failures"].extend(
        {
            "stage": "search",
            "query": row.query,
            "status": row.status,
            "error": dict(row.error) if row.error is not None else None,
        }
        for row in search_outcomes
        if row.status != "success"
    )

pool = sorted(pool_by_source.values(), key=lambda row: row.get("rank", 1_000_000))[:300]
sdk.state.write_jsonl(pool_path, pool)

fetch_batch = 4
already_fetched = set(meta["fetched_sources"])
selected = []
seen_families = set()
for candidate in new_candidates:
    family = candidate.get("domain") or candidate["source"]
    if candidate["source"] in already_fetched or family in seen_families:
        continue
    selected.append(candidate)
    seen_families.add(family)
    if len(selected) >= fetch_batch:
        break

sources = [row["source"] for row in selected]
if sources:
    documents = {}
    for source in sources:
        try:
            document = sdk.content.fetch(source)
        except BrokerError as error:
            meta["failures"].append({"stage": "fetch", "source": source, "code": error.code})
        else:
            documents[document.source] = document
            already_fetched.add(document.source)

    meta["fetched_sources"] = sorted(already_fetched)

    passage_report = sdk.content.passages(
        task,
        sources=list(documents),
        limit=10,
        limit_per_source=2,
    )
    content_by_key = {row["key"]: row for row in content}
    for passage in passage_report.passages:
        coordinates = dict(passage.coordinates)
        start = coordinates["start_line"]
        end = coordinates["end_line"]
        key = f"{passage.source}#L{start}-{end}"
        content_by_key[key] = {
            "key": key,
            "source": passage.source,
            "title": passage.title,
            "text": passage.text,
            "coordinates": coordinates,
        }
    meta["failures"].extend(
        {"stage": "passages", "source": row.source, "code": row.code}
        for row in passage_report.failures
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

The example only considers the current targeted search batch, then keeps one candidate per source
family before fetching. To inspect older pooled candidates, make that semantic choice explicitly in
the next adapted program. `meta.fetched_sources` prevents an unchanged replay from fetching the same
source again; persist full document text only when repeated local processing justifies its workspace
cost.

After an adapter failure with unknown outcome, inspect the existing cache and session usage before
choosing new work. After explicit `state_lost`, rebuild state and re-admit local IDs; public URLs
remain reusable. Use `sdk.output.submit(...)` only when structured runtime output is requested.
