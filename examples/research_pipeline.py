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
    for candidate in fusion.candidates
    if candidate.domain in official_domains
}

snippets = sdk.content.snippets(
    "vector index types, filtering, consistency and limitations",
    list(unique),
    max_tokens_per_page=800,
)

sdk.state.write_jsonl("evidence.jsonl", [item.model_dump() for item in snippets])
sdk.output.submit(
    {"evidence": [item.model_dump() for item in snippets]},
    citations=[
        {"ref": item.ref, "locator": item.locator}
        for item in snippets
        if item.locator is not None
    ],
)
