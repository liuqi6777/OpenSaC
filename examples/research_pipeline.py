from opensac_sdk import sdk

queries = [
    "PostgreSQL vector search official documentation",
    "Elasticsearch vector search official documentation",
    "Milvus vector search official documentation",
]

official_domains = {"postgresql.org", "github.com", "elastic.co", "milvus.io"}
search_report = sdk.search.many(queries, limit_per_query=10, concurrency=3)
fusion = sdk.search.fuse_rrf(
    search_report,
    k=60,
    limit=24,
    domain_weights={domain: 1.5 for domain in official_domains},
    max_per_domain=6,
)

unique = {
    candidate.source: candidate for candidate in fusion if candidate.domain in official_domains
}

report = sdk.content.passages(
    "vector index types, filtering, consistency and limitations",
    list(unique),
    limit=20,
    max_per_source=3,
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
    citations=list(dict.fromkeys(item.source for item in report.passages)),
)
