# Fan-out and state

Read when batching queries, combining rankings or continuing from saved results.
See [fanout_research.py](../examples/fanout_research.py) for a complete save-and-resume example.

## Batch alignment

Use `zip(inputs, results, strict=True)` before filtering so failed-item identity is retained.
Search batches return one `BatchItem[list[SearchHit]]` per query; fetch batches return one
`BatchItem[Document]` per URL. Both preserve input order and duplicates. Check `.ok`, then `.data`
or `.error.code`. An empty successful hit list is completed work.

Both methods accept 1–32 inputs per call. Chunk larger workloads explicitly and save each completed
chunk. Invalid arguments may fail a whole call; unexpected programming errors can propagate too.
There are no automatic retries. Save failed inputs separately and retry deliberately after
addressing their cause or deciding a bounded retry is useful.

## Local composition

```python
from opensac import dedup, fuse

unique = dedup(hits)
ranked = fuse(result_lists)
```

- `dedup(items, *, key=None)` keeps the first item per identity, preserving input order.
- `fuse(result_sets, *, weights=None, k=60, key=None)` performs weighted reciprocal rank fusion
  using list order. It returns original objects rather than new records with scores.

Default identity is `.url`, compared exactly without normalization. One URL contributes once per
list; equal fusion scores retain first-seen order. For saved dicts, either restore models or pass
`key=lambda row: row["url"]`. Both run locally without network requests; neither merges metadata nor adds provenance.
Keep a separate URL-to-query mapping when tracing which queries found each candidate.

If supplying weights, align them with the lists actually passed after handling failures. Weights
must be finite and nonnegative with at least one positive value for nonempty input. Leave `k=60`
unless the task calls for different weighting. Slice the returned list to select a candidate pool;
there is no `limit`, domain filter or per-domain cap argument on these functions.

## Save and resume

SDK models need `.model_dump(mode="json")` or `.model_dump_json()` before JSON serialization.
`json.loads` produces dicts; use `SearchHit.model_validate(row)` to restore search hits.
`BatchItem.ok` is computed and is not stored in JSON.

Choose a path scoped to the task and freshness requirements. Save successful
empty batches as well as nonempty ones. Retain unresolved errors instead of replaying them on every
invocation. The example records both and retries a failed query only after its saved error entry is
removed deliberately. New queries can be added without repeating completed ones.

Files are local; different Python processes do not share variables. Reuse saved data when it still
addresses the question. Refresh deliberately when sources or retrieval requirements change. A completed
search is not necessarily a resolved research question; use evidence gaps to decide the next action.

## Coverage

Validate upstream membership and keys before deriving downstream inputs. One input can produce
zero, one or several in-scope records; do not collapse that multiplicity accidentally. Missing
fields and failed inputs remain gaps. Claim completeness only within an evidence-supported scope,
not merely because every batch finished. Preserve valid `0` and `False` values when processing data.
