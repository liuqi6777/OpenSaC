from opensac_sdk import sdk

queries = [
    "milvus vector search",
    "hnsw index",
    "approximate nearest neighbor search",
]

batches = sdk.search.many(queries, limit_per_query=5, concurrency=3)

failed = [batch for batch in batches if batch.error]
if len(failed) == len(batches):
    raise RuntimeError(f"Every local search failed: {failed[0].error}")
for batch in failed:
    print(f"warning: '{batch.query}' failed: {batch.error}")

# The local backend keys documents by docid, so deduplicate on it and keep the
# highest scoring hit for each document.
best: dict[str, object] = {}
for batch in batches:
    for hit in batch.hits:
        current = best.get(hit.docid)
        if current is None or (hit.score or 0) > (current.score or 0):
            best[hit.docid] = hit

ranked = sorted(best.values(), key=lambda hit: hit.score or 0, reverse=True)[:5]
print(f"{len(ranked)} unique documents from {len(queries)} queries")

report = sdk.content.passages(
    "vector index types and their tradeoffs",
    [hit.ref for hit in ranked],
    limit=15,
    max_per_ref=3,
)

sdk.state.write_jsonl(
    "evidence.jsonl",
    [item.model_dump(mode="json") for item in report.passages],
)
sdk.output.submit(
    {
        "documents": [
            {"docid": hit.docid, "score": hit.score, "snippet": hit.snippet[:200]} for hit in ranked
        ],
        "evidence_chars": sum(len(item.text) for item in report.passages),
        "fetch_failures": [item.model_dump(mode="json") for item in report.failures],
    },
    citations=[
        {"ref": item.ref, "locator": item.locator}
        for item in report.passages
        if item.locator is not None
    ],
)
