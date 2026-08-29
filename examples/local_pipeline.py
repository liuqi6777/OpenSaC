from opensac_sdk import sdk

queries = [
    "milvus vector search",
    "hnsw index",
    "approximate nearest neighbor search",
]

search_outcomes = sdk.search.many(queries, limit=5, concurrency=3)

failures = [outcome for outcome in search_outcomes if outcome.status != "success"]
if len(failures) == len(search_outcomes):
    error = failures[0].error
    detail = error.message if error is not None else "unknown search failure"
    raise RuntimeError(f"Every local search failed: {detail}")
for failure in failures:
    error = failure.error
    code = error.code if error is not None else "unknown"
    message = error.message if error is not None else "search failed"
    print(f"warning: '{failure.query}' failed: code={code} message={message}")

# A local source is its document ID. Sorting low-to-high makes each later
# duplicate replace its lower-scoring predecessor in the comprehension.
all_hits = [
    hit for outcome in search_outcomes if outcome.status == "success" for hit in outcome.hits
]
best = {hit.source: hit for hit in sorted(all_hits, key=lambda candidate: candidate.score or 0)}

ranked = sorted(best.values(), key=lambda hit: hit.score or 0, reverse=True)[:5]
print(f"{len(ranked)} unique documents from {len(queries)} queries")

report = sdk.content.passages(
    "vector index types and their tradeoffs",
    sources=[hit.source for hit in ranked],
    limit=15,
    limit_per_source=3,
)

sdk.workspace.write_jsonl(
    "evidence.jsonl",
    [dict(item) for item in report.passages],
)
for item in report.passages[:8]:
    excerpt = " ".join(item.text.split())[:400]
    print(f"EVIDENCE source={item.source!r} text={excerpt!r}")
print(
    f"READY: documents={len(ranked)} evidence={len(report.passages)} "
    f"failures={len(report.failures)} artifact='evidence.jsonl'"
)
