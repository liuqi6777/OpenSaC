"""Batch, deduplicate, save, and resume; send this body to agent-run on stdin.

Requires session persistence to reuse files across calls. Adapt QUERIES and STATE_PATH
for each task. Add queries for evidence gaps; change the path to intentionally refresh
completed searches. Candidates still need document inspection before supporting claims.
"""

import json
from pathlib import Path

from opensac_sdk import sdk

QUERIES = ["Python 3.13 free threading", "Python 3.13 JIT compiler"]
STATE_PATH = Path("python-313-candidates.json")
state = json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {"batches": {}}
# Failed queries may be retried on a later invocation once their cause is addressed.
pending = list(dict.fromkeys(q for q in QUERIES if q not in state["batches"]))
results = sdk.search.many(pending, limit=5) if pending else []
failed = []
for query, hits in zip(pending, results, strict=True):
    if hits is None:
        failed.append(query)
    else:
        state["batches"][query] = hits

pool = {}
for query, hits in state["batches"].items():
    for hit in hits:
        source = hit["source"]
        if source not in pool:
            pool[source] = {**dict(hit), "queries": []}
        pool[source]["queries"].append(query)
state["candidates"] = list(pool.values())
state["failed_queries"] = failed
STATE_PATH.write_text(json.dumps(state, ensure_ascii=False))
print(
    f"Saved {len(pool)} sources to {STATE_PATH}; "
    f"{len(pending)} queries searched this run, {len(failed)} failed."
)
if failed:
    print("Failed queries: " + "; ".join(failed))
