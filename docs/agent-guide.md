# OpenSaC for agents

Draft for a future standalone skill. This file is repository documentation, not part of the Python API.

Use OpenSaC in your existing Python environment. Variables and files belong to that environment.
The SDK runs locally without an execution session, document registry, MCP call or OpenSaC server.
The host configures provider endpoints and credentials.

```python
from opensac import sdk, fuse, dedup
```

## Capabilities and cost

- `sdk.search(query, limit=5)` returns a list of `SearchHit(url, title, snippet, domain, date)`.
- `sdk.search.many(queries, limit=5)` returns one BatchItem per input query.
- `sdk.content.fetch(url)` returns `Document(url, text)`. Search is not a prerequisite.
- `sdk.content.fetch_many(urls)` returns one BatchItem per URL.
- `sdk.rerank(query, items, top_n=None, text=None)` returns the original objects reordered.
  SearchHit uses title + snippet; Document uses text. Custom objects need `text=lambda item: ...`.
- `sdk.llm.complete(prompt)` returns `.text`, `.model` and optional `.usage`.
- `sdk.llm.extract(text, schema, instruction=...)` returns validated `.data` and model/usage metadata.
- `sdk.llm.complete_many(prompts, ...)` and `sdk.llm.extract_many(texts, schema, ...)` return BatchItems.
- `fuse(result_lists, weights=None, k=60)` and `dedup(items)` are local, model-free functions.

Search, fetch, rerank and LLM operations may make external requests and incur provider charges.
Fusion and URL deduplication do not. Rerank does not fetch full text automatically. Extraction calls
an LLM; it is not a local text parser. Prefer ordinary Python for exact matching and filtering.

## Choose operations to address missing evidence

Start with a focused search. Add alternative queries when the first results miss a relevant aspect;
there is no mandatory number of formulations. Inspect a few titles/snippets before fetching bodies.
Use fusion when combining ranked lists is useful; adding a weak list can hurt ranking quality.
Use rerank to select from a candidate pool when relevance is difficult to judge from the first ranking.
Use extraction when structured interpretation is needed, rather than for information Python can parse.
Search limits are at most 100, batches at most 32. Rerank accepts at most 100 texts and 500,000 total
characters. Plan larger workloads explicitly; the SDK does not silently split, truncate or retry them.

## Compose results and preserve failures

```python
query = "retrieval evaluation"
batches = sdk.search.many([query, "evaluating retrieval quality"], limit=10)
lists = []
for batch in batches:
    if batch.error is not None:
        print("Search failed:", batch.error.code)
    else:
        lists.append(batch.data)

pool = fuse(lists)  # exact URL identity; ordered best first
if pool:
    best = sdk.rerank(query, pool[:100], top_n=min(5, len(pool)))
    documents = sdk.content.fetch_many([hit.url for hit in best])
    for hit, batch in zip(best, documents):
        if batch.error is not None:
            print(hit.url, batch.error.code)
        else:
            print(batch.data.url, batch.data.text[:500])  # a preview, not the full document
```

`fuse` uses list position; one URL contributes once
per list. It preserves the first original object from contributing lists, including its metadata.
Equal fusion scores retain first-seen order. `dedup` preserves the first item per exact URL. Neither
normalizes URLs or merges distinct query strings. Both accept a custom `key` callback.
`sdk.rerank` preserves object identity and metadata. The returned list order is the new ranking.
Empty input returns [] locally.

## Structured extraction

```python
schema = {
    "type": "object",
    "properties": {"title": {"type": "string"}},
    "required": ["title"],
    "additionalProperties": False,
}
result = sdk.llm.extract("Title: Retrieval Evaluation", schema)
print(result.data["title"])
```

The configured backend must support strict JSON Schema output. The core validates output locally
against a [simple bounded subset](models.md); references, regex and composition are unsupported.
It does not coerce types or repair
invalid JSON. A refusal, truncated generation or schema mismatch raises a distinct OpenSACError.

Use `if r.ok:` to check batch success, including empty search results.
Single-call failures raise `OpenSACError`; expected batch failures appear in the matching `.error`.
An invalid batch envelope fails the whole request. Empty successful searches are valid results.
Do not treat errors as empty results or assume retrying an uncertain request is free.
Use original URLs in saved results and citations. Save full bodies and JSON to local files when useful;
print only the evidence needed for the next reasoning step. After active calls finish, `sdk.close()`
releases resources. It can be used again; use `with Client(...) as sac:` for an explicit client scope.
