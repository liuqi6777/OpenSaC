# Fan-out and state reference

Read when batching queries, combining rankings, or continuing from saved results. See
[fanout_research.py](../examples/fanout_research.py) for batch processing with saved results.

## Batch calls

Common options:

```text
sdk.search.many(
    queries, *, limit=10, concurrency=5, include_domains=None
) -> list[list[record] | None]
```

`queries` is a list of strings. Other parameters apply to every query; search parameters have the
same meaning as in [search.md](search.md). `concurrency` bounds fan-out within the invocation.

Keep the original inputs. Results preserve order, duplicates, and partial success, including an
aligned list of `None` when every operation fails. Use `zip(inputs, results, strict=True)` before
filtering so failed-item identity is retained. An empty successful hit list is a completed search.
`content.fetch_many` follows the same alignment contract; its signature is in [content.md](content.md).

## Local ranking fusion

```text
sdk.search.fuse_rrf(
    queries, results, *, weights=None, k=60, limit=None, exclude_domains=None,
    domain_weights=None, max_per_domain=None
) -> list[record]
```

Pass original hit records and aligned results, including failed positions. Fusion runs locally
and combines rankings by source; it does not issue another search.

- `weights` is a list aligned with every input query, including failed positions. Values are
  non-negative, with at least one positive value; omitted weights are equal.
- `k` controls rank smoothing; keep the default unless the task calls for different weighting.
- `limit` truncates the final fused list.
- `exclude_domains` removes candidates and `domain_weights` maps domains to multipliers. Both
  match the specified hostname and its subdomains.
- `max_per_domain` caps candidates per exact hostname before the final limit is applied.

Fused records retain the hit fields and add `provenance`. Use its `input_index` and `query` to
trace a candidate to the original inputs. Other ranking fields can remain internal to the SDK.

## Save and resume

Use `pathlib` and `json` in the program working directory. Choose task-specific paths; `.opensac-*`
files belong to the runtime. SDK records serialize with `json`; loaded records are plain dicts.

Load saved batches before deriving new inputs. Merge candidates by source while retaining which
queries found them. Save successful empty batches too, and retain unresolved inputs separately.
Write state directly to the task file when a later call will reuse it. A completed search is not
necessarily a resolved research question: use evidence gaps to decide whether new queries or
document inspection are needed.

Read existing results before repeating external work. Session persistence and state-loss behavior
are described in the root skill.

## Enumeration and downstream inputs

Validate upstream membership and stable keys before deriving downstream inputs. One unit may yield
zero, one, or several records; preserve every in-scope record rather than collapsing to one row per
unit. A processed batch does not establish exhaustive coverage. Claim completeness only within a
scope supported by inspected evidence; missing fields, failed inputs, and unresolved mentions remain
gaps. Preserve valid `0` and `False` values instead of treating them as missing.
