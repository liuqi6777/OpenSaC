from opensac_sdk import sdk

queries = [
    "PostgreSQL vector search official documentation",
    "Elasticsearch vector search official documentation",
    "Milvus vector search official documentation",
]

batches = sdk.search.many(queries, limit_per_query=8, concurrency=3)
fusion = sdk.search.fuse_rrf(batches, k=60, limit=24)
official_domains = {"postgresql.org", "github.com", "elastic.co", "milvus.io"}

unique = {
    candidate.ref: candidate
    for candidate in fusion
    if candidate.domain in official_domains
}

report = sdk.content.passages(
    "vector index types, filtering, consistency and limitations",
    list(unique),
    limit=20,
    max_per_ref=3,
)

sdk.state.write_jsonl(
    "evidence.jsonl",
    [dict(item) for item in report.passages],
)
sdk.output.submit(
    {
        "evidence": [dict(item) for item in report.passages],
        "fetch_failures": [dict(item) for item in report.failures],
    },
    citations=[
        {"ref": item.ref, "locator": item.locator}
        for item in report.passages
        if item.locator is not None
    ],
)
