"""Save and resume independent searches using local files.

Adapt queries and the state path for the task. Reuse a path only with the same
task scope and freshness requirements. Failed queries stay recorded;
remove their error entries to retry deliberately after addressing the cause.
"""

import json
from pathlib import Path

from opensac import fuse, sdk
from opensac.contracts import SearchHit

QUERIES = ["Python 3.13 free threading", "Python 3.13 JIT compiler"]
LIMIT = 5
STATE_PATH = Path("python-313-candidates.json")

state = (
    json.loads(STATE_PATH.read_text(encoding="utf-8"))
    if STATE_PATH.exists()
    else {"limit": LIMIT, "batches": {}, "errors": {}}
)
if state["limit"] != LIMIT:
    raise ValueError("Use a new state path when changing the search limit.")
pending = list(
    dict.fromkeys(q for q in QUERIES if q not in state["batches"] and q not in state["errors"])
)
try:
    for start in range(0, len(pending), 32):
        queries = pending[start : start + 32]
        results = sdk.search.many(queries, limit=LIMIT)
        for query, result in zip(queries, results, strict=True):
            if result.ok:
                state["batches"][query] = [hit.model_dump(mode="json") for hit in result.data]
            else:
                state["errors"][query] = result.error.model_dump(mode="json")
        # Save each completed chunk so a later failure does not discard earlier work.
        STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    ranked_lists = []
    provenance = {}
    for query in dict.fromkeys(QUERIES):
        if query not in state["batches"]:
            continue
        hits = [SearchHit.model_validate(row) for row in state["batches"][query]]
        ranked_lists.append(hits)
        for hit in hits:
            provenance.setdefault(hit.url, []).append(query)
    pool = fuse(ranked_lists)
    failed = [q for q in dict.fromkeys(QUERIES) if q in state["errors"]]
    print(f"{len(pool)} candidates; {len(failed)} unresolved queries; state: {STATE_PATH}")
    for query in failed:
        print(query, state["errors"][query]["code"])
    for hit in pool[:8]:
        print(hit.title, hit.url, provenance[hit.url])
finally:
    sdk.close()
