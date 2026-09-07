# Search

Read when composing searches or interpreting candidates. For batching, fusion and resuming work,
see [fan-out and state](fanout.md).

```python
sdk.search(query, *, limit=5)          # list[SearchHit]
sdk.search.many(queries, *, limit=5)    # list[BatchItem[list[SearchHit]]]
```

`query` is a string of 1–2,000 characters after trimming; `limit` is an integer from 1 to 100.
Batch inputs are lists of 1–32 strings. Chunk larger sets explicitly and skip empty batches.
The same `limit` applies to every query in a batch. Search accepts only `query` and `limit`.

Use this same interface for all searches. Filtering candidates by domain in Python only filters
returned results and cannot establish exhaustive coverage of a site.

## Query design

Start with a concise phrase naming the subject and the evidence needed. Include discriminating
terms such as a product version, organization, place or year when they change the answer. Avoid
pasting the entire user request. There is no fixed word count: retain necessary context.

Split independent information needs into separate queries. For example:

```python
queries = [
    "Python 3.13 free threading limitations",
    "Python 3.13 experimental JIT support",
]
results = sdk.search.many(queries, limit=5)
```

For multi-entity questions, search each entity separately when each needs its own evidence. Keep
both names together when their relationship is the question, such as a direct comparison or an
agreement between organizations. Batch independent searches; wait for upstream results before
forming queries whose entities or terminology depend on them.

Target the desired evidence: use the version and API name for implementation behavior, or the
reporting period and metric for official figures. Prefer primary sources for claims about their
own products or data; seek independent sources when evaluation is needed. Check returned URLs
and documents for authority rather than relying on the word "official" in a query.

Refine from titles and snippets. Narrow noisy results with a distinguishing term, broaden empty
results by removing an unnecessary constraint, and resolve ambiguous names before drawing
conclusions. Add reformulations for a concrete retrieval gap, not a fixed quota of synonyms.
A year in the query helps focus retrieval but does not enforce a publication-date filter.

Put essential retrieval intent in the query and keep selection criteria in surrounding Python
and reasoning. OpenSaC has no `intent` parameter. Increasing `limit` can reveal more candidates but does not resolve an
ambiguous question or establish complete coverage.

## Hit fields

| Attribute | Meaning |
| --- | --- |
| `url` | HTTP(S) URL to pass to fetch and retain with evidence |
| `title` | Result title |
| `snippet` | Candidate preview, not the full document |
| `domain` | Optional domain; defaults to the URL hostname when absent |
| `date` | Optional date text, not necessarily a normalized publication date |

Hits are models, not dicts. Use `hit.url`, not `hit["source"]`. Search returns a list directly,
without a `.results` wrapper or a rank field. List order expresses ranking.

For JSON, use `hit.model_dump(mode="json")` or `hit.model_dump_json()`. Restore a saved dict with
`SearchHit.model_validate(row)`, importing `SearchHit` from `opensac.contracts`.

## Outcomes

An empty list is successful. A single search failure raises `OpenSACError` from `opensac.errors`.
For a batch, use `if result.ok:`, then `result.data`; on failure inspect `result.error.code`.
Invalid arguments can fail the whole batch before retrieval. Keep failures separate from empty
successful searches, and use result feedback to decide whether a new query is useful.
