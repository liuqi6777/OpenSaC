from opensac_sdk import sdk

queries = [
    "milvus vector search",
    "hnsw index",
    "approximate nearest neighbor search",
]

batches = sdk.search.many(queries, limit_per_query=5, concurrency=3)

failed = [batch for batch in batches if batch.failure]
if len(failed) == len(batches):
    raise RuntimeError(f"Every local search failed: {failed[0].failure}")
for batch in failed:
    print(f"warning: '{batch.query}' failed: {batch.failure}")

# A local source is its document ID. Keep the highest-scoring hit per source.
best: dict[str, object] = {}
for batch in batches:
    for hit in batch.hits:
        current = best.get(hit.source)
        if current is None or (hit.score or 0) > (current.score or 0):
            best[hit.source] = hit

ranked = sorted(best.values(), key=lambda hit: hit.score or 0, reverse=True)[:5]
print(f"{len(ranked)} unique documents from {len(queries)} queries")

report = sdk.content.passages(
    "vector index types and their tradeoffs",
    [hit.source for hit in ranked],
    limit=15,
    max_per_source=3,
)

sdk.state.write_jsonl(
    "evidence.jsonl",
    [dict(item) for item in report.passages],
)
sdk.output.submit(
    {
        "documents": [
            {"source": hit.source, "score": hit.score, "snippet": hit.snippet[:200]}
            for hit in ranked
        ],
        "evidence_chars": sum(len(item.text) for item in report.passages),
        "fetch_failures": [dict(item) for item in report.failures],
    },
    citations=list(dict.fromkeys(item.source for item in report.passages)),
)
