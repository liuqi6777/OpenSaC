from opensac_sdk import sdk

queries = [
    "milvus vector search",
    "hnsw index",
    "approximate nearest neighbor search",
]

search_results = sdk.search.many(queries, limit=5, concurrency=3)
search_failure_count = sum(result is None for result in search_results)

# A local source is its document ID. Sorting low-to-high makes each later
# duplicate replace its lower-scoring predecessor in the comprehension.
all_hits = [hit for result in search_results if result is not None for hit in result]
best = {hit.source: hit for hit in sorted(all_hits, key=lambda candidate: candidate.score or 0)}

ranked = sorted(best.values(), key=lambda hit: hit.score or 0, reverse=True)[:5]
print(f"{len(ranked)} unique documents from {len(queries)} queries")

report = sdk.content.passages(
    "vector index types and their tradeoffs",
    sources=[hit.source for hit in ranked],
    limit=15,
    limit_per_source=3,
)
passages = report.passages if report is not None else []
failures = report.failures if report is not None else []

sdk.workspace.write_jsonl(
    "evidence.jsonl",
    [dict(item) for item in passages],
)
for item in passages[:8]:
    excerpt = " ".join(item.text.split())[:400]
    print(f"EVIDENCE source={item.source!r} text={excerpt!r}")
print(
    f"READY: documents={len(ranked)} evidence={len(passages)} "
    f"failures={search_failure_count + len(failures)} artifact='evidence.jsonl'"
)
