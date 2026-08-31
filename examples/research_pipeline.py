from opensac_sdk import sdk

queries = [
    "PostgreSQL vector search official documentation",
    "Elasticsearch vector search official documentation",
    "Milvus vector search official documentation",
]

official_domains = {"postgresql.org", "github.com", "elastic.co", "milvus.io"}
search_results = sdk.search.many(queries, limit=10, concurrency=3)
fusion = sdk.search.fuse_rrf(
    queries,
    search_results,
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
    sources=list(unique),
    limit=20,
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
    print(
        f"EVIDENCE source={item.source!r} coordinates={dict(item.coordinates)!r} text={excerpt!r}"
    )
print(f"READY: evidence={len(passages)} failures={len(failures)} artifact='evidence.jsonl'")
