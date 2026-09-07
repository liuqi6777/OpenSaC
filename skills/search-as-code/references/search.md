# Search

Read when composing searches or interpreting candidates.

```python
sdk.search(query, *, limit=5)           # list[SearchHit]
sdk.search(reformulations, *, limit=5)  # one fused list[SearchHit]
```

`query` is a string of 1–2,000 characters after trimming. A list passed directly to `search` contains
1–10 reformulations of one intent. Exact duplicate strings are requested once; the rankings are fused
locally and the returned list is capped by one shared `limit`. If any reformulation fails, the logical
search raises that failure rather than silently returning partial evidence. `limit` is an integer
from 1 to 100. Search accepts only the query input and `limit`.

Use this same interface for all searches. Filtering candidates by domain in Python only filters
returned results and cannot establish exhaustive coverage of a site.

## Query design

Start with a concise phrase naming the subject and the evidence needed. Include discriminating
terms such as a product version, organization, place or year when they change the answer. Avoid
pasting the entire user request. There is no fixed word count: retain necessary context.

Use one query unless alternate wording resolves an actual ambiguity or retrieval gap. When variants
are useful, pass them together to `search([...])` so they produce one fused candidate list. Do not
generate a fixed quota of variants.

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

An empty list is successful. A search failure raises `OpenSACError` from `opensac.errors`. Use result
feedback to decide whether a new query or reformulation is useful.
