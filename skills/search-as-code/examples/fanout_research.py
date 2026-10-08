"""Save and resume independent searches using local files.

Adapt queries and the state path for the task. Reuse a path only with the same
task scope and freshness requirements. Failed queries stay recorded;
remove their error entries to retry deliberately after addressing the cause.
"""

import json
from pathlib import Path

from opensac import dedup, sdk
from opensac.contracts import SearchHit

QUERIES = ["Python 3.13 free threading", "Python 3.13 JIT compiler"]
LIMIT = 5
CANDIDATES_PER_QUERY = 2  # A preview size; adapt selection to relevance and evidence gaps.
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

    selected: list[SearchHit] = []
    provenance: dict[str, list[str]] = {}
    print(f"Saved search results: {STATE_PATH}")
    for query in dict.fromkeys(QUERIES):
        if query not in state["batches"]:
            print(query, "search failed:", state["errors"][query]["code"])
            continue
        hits = [SearchHit.model_validate(row) for row in state["batches"][query]]
        for hit in hits:
            sources = provenance.setdefault(hit.url, [])
            if query not in sources:
                sources.append(query)
        if not hits:
            print(query, "no candidates; evidence gap remains")
            continue
        candidates = dedup(hits)[:CANDIDATES_PER_QUERY]
        selected.extend(candidates)
        print(query, f"{len(hits)} hits; previewing {len(candidates)} candidates:")
        for hit in candidates:
            print(hit.title, hit.url)
    # Fetch each selected URL once, while retaining every query that found it.
    fetch_urls = [hit.url for hit in dedup(selected)]
    print(f"{len(fetch_urls)} distinct URLs selected for inspection; evidence is not yet verified")
finally:
    sdk.close()
