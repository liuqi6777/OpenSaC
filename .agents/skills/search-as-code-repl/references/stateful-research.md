# Live namespace and lightweight recovery state

The persistent namespace is the default working memory. Use `sdk.state` only when a durable cache
would save meaningful external work after interpreter loss or support a later program. Files and live
Python objects are independent; do not mirror the whole namespace after every cell.

## Small durable data model

When recovery value justifies persistence, three cumulative artifacts are usually enough:

| Artifact | Reusable data |
| --- | --- |
| `meta.json` | Task, attempted queries, bounded failures or coverage |
| `pool.jsonl` | Normalized candidates, deduplicated by `source` |
| `content.jsonl` | Inspected windows with source, coordinates, and text |

This is a data cache, not a workflow state machine. Do not create per-cell logs, `round2` or `stage3`
files, duplicate raw reports, or a final ledger unless the task needs them. Keep values used only by
the next live cell in Python.

Confirm `sdk.session.capabilities()["mechanisms"]["persistence"]` before relying on files across
cells. With persistence disabled, each cell gets a temporary workspace even though Python globals
remain alive.

## Optionally checkpoint useful live rows

Run this after a composed retrieval cell only when recovery is worth the I/O. It merges bounded live
rows into the same cache files instead of encoding cell progression in filenames.

```python
import hashlib

from opensac_sdk import sdk

capabilities = sdk.session.capabilities()
persistence_enabled = bool(capabilities["mechanisms"]["persistence"])
if not persistence_enabled:
    print("CACHE disabled; continue with the live namespace")
else:
    goal = research_goal if "research_goal" in globals() else "replace with the evidence question"
    research_id = hashlib.sha256(goal.encode()).hexdigest()[:12]
    cache_root = f"runs/{research_id}"
    meta_path = f"{cache_root}/meta.json"
    pool_path = f"{cache_root}/pool.jsonl"
    content_path = f"{cache_root}/content.jsonl"

    meta = dict(sdk.state.read_json(meta_path)) if sdk.state.exists(meta_path) else {
        "goal": goal,
        "queries": [],
    }
    existing_pool = (
        [dict(row) for row in sdk.state.read_jsonl(pool_path)]
        if sdk.state.exists(pool_path)
        else []
    )
    existing_content = (
        [dict(row) for row in sdk.state.read_jsonl(content_path)]
        if sdk.state.exists(content_path)
        else []
    )

    live_queries = queries if "queries" in globals() else []
    meta["queries"] = list(dict.fromkeys([*meta["queries"], *live_queries]))[-100:]
    pool_by_source = {row["source"]: row for row in existing_pool}
    for row in candidate_pool if "candidate_pool" in globals() else []:
        pool_by_source[row.source] = {
            "source": row.source,
            "title": row.title,
            "domain": row.domain,
            "date": row.date,
            "snippet": (row.snippet or "")[:500],
            "rank": row.fused_rank,
            "score": row.fused_score,
        }

    content_by_key = {row["key"]: row for row in existing_content}
    for row in evidence_windows if "evidence_windows" in globals() else []:
        start = row["coordinates"].get("start_line")
        end = row["coordinates"].get("end_line")
        key = f"{row['source']}#L{start}-{end}"
        content_by_key[key] = {
            "key": key,
            "source": row["source"],
            "title": row["title"],
            "text": row["text"],
            "coordinates": row["coordinates"],
        }

    cached_pool = list(pool_by_source.values())[-300:]
    cached_content = list(content_by_key.values())[-300:]
    sdk.state.write_json(meta_path, meta)
    sdk.state.write_jsonl(pool_path, cached_pool)
    sdk.state.write_jsonl(content_path, cached_content)
    print(
        f"CACHE research={research_id} pool={len(cached_pool)} "
        f"content={len(cached_content)}"
    )
```

## Inspect before replay or recover after loss

After an adapter failure with unknown outcome, first inspect relevant globals and
`sdk.session.usage()` in a read-only cell. Do not repeat an external call merely because its
observation was lost.

After explicit `interpreter_state=lost` or `state_lost`, the next invocation starts with a clean
namespace. Load a trustworthy cache only when persistence was enabled. Print bounded cached excerpts
for the next judgment; re-admit local IDs through search before using them again.

```python
from opensac_sdk import sdk

research_id = "copy-the-task-derived-id"
cache_root = f"runs/{research_id}"
meta_path = f"{cache_root}/meta.json"
pool_path = f"{cache_root}/pool.jsonl"
content_path = f"{cache_root}/content.jsonl"

cached_meta = dict(sdk.state.read_json(meta_path)) if sdk.state.exists(meta_path) else {}
cached_pool = sdk.state.read_jsonl(pool_path) if sdk.state.exists(pool_path) else []
cached_content = sdk.state.read_jsonl(content_path) if sdk.state.exists(content_path) else []

print(
    f"RECOVERY research={research_id} pool={len(cached_pool)} "
    f"content={len(cached_content)} queries={len(cached_meta.get('queries', []))}"
)
for row in cached_content[-4:]:
    excerpt = " ".join(row.text.split())[:500]
    print(f"EVIDENCE source={row.source!r} excerpt={excerpt!r}")
print("NEXT: judge cached evidence, re-admit local IDs, or resume only missing work")
```

Public web URLs remain reusable after recovery. Never infer completion merely because a capability
call appeared in the lost cell's source; require live or cached evidence of its result.
