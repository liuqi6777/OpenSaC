from opensac_sdk import sdk

queries = [
    "PostgreSQL vector search official documentation",
    "Elasticsearch vector search official documentation",
    "Milvus vector search official documentation",
]

batches = sdk.search.web_many(queries, limit_per_query=8, concurrency=3)
official_domains = {"postgresql.org", "github.com", "elastic.co", "milvus.io"}

unique = {}
for batch in batches:
    for hit in batch.hits:
        if hit.domain in official_domains:
            unique.setdefault(hit.url, hit)

snippets = sdk.content.snippets(
    "vector index types, filtering, consistency and limitations",
    [hit.ref for hit in unique.values()],
    max_tokens_per_page=800,
)

sdk.state.write_jsonl("evidence.jsonl", [item.model_dump() for item in snippets])
sdk.output.submit(
    {"evidence": [item.model_dump() for item in snippets]},
    citations=[{"ref": item.ref} for item in snippets if item.ref],
)
