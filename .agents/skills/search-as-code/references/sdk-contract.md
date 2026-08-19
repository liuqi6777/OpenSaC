# OpenSAC SDK contract

Use this reference when exact signatures, fields, limits, or failure semantics matter.
Import only `BrokerError` and `sdk` from `opensac_sdk`. Structured results are ordinary JSON
records: both `row.ref` and `row["ref"]` read the same field, and `dict(row)` serializes it.

## Contents

- [Capabilities](#capabilities)
- [Exact result fields](#exact-result-fields)
- [Failure and alignment semantics](#failure-and-alignment-semantics)
- [Retrieval and evidence limits](#retrieval-and-evidence-limits)
- [Workspace state, output, and lifecycle](#workspace-state-output-and-lifecycle)
- [Sandbox constraints](#sandbox-constraints)

## Core and helper capabilities

Search:

```python
sdk.search(query, limit=10, offset=0, domains=None) -> list[record]
sdk.search.many(
    queries, limit_per_query=10, offset=0, concurrency=5, domains=None
) -> list[record]
sdk.search.fuse_rrf(
    batches, weights=None, k=60, limit=None
) -> list[record]
```

`fuse_rrf` is deterministic local Python and makes no RPC.

Content core:

```python
sdk.content.passages(
    query, refs, limit=20, max_per_ref=3
) -> record
sdk.content.grep_report(
    refs, pattern, context=0, max_matches_per_ref=20
) -> record
sdk.content.read(
    refs, offset=1, limit=200, max_chars=100_000
) -> list[record]
```

Session, state, and output:

```python
sdk.session.usage() -> dict
sdk.state.write_json(path, value)
sdk.state.write_jsonl(path, rows)
sdk.state.append_jsonl(path, rows)
sdk.state.merge_jsonl(path, rows, key="ref") -> int
sdk.state.read_json(path)
sdk.state.read_jsonl(path)
sdk.state.exists(path) -> bool
sdk.state.list(prefix="") -> list[str]
sdk.output.submit(output, citations=[{"ref": ref, "locator": locator}])
```

Structured extraction is an optional deployment capability:

```python
sdk.llm.extract_many(
    items,
    instruction=...,
    schema=...,
    concurrency=4,
    max_tokens=None,
    repair_attempts=0,
) -> list[record]
```

The `extract_many` schema must be a JSON-serializable object whose root type is `object`.
`repair_attempts` accepts only `0` or `1`. A missing pipeline-model configuration raises a broker
error; keep a deterministic fallback.

## Exact result fields

- Search hit: `ref`, `backend`, `title`, `url`, `docid`, `domain`, `date`, `snippet`, `score`,
  `rank`, `retrieval`, `metadata`.
- Search batch: `query`, `hits`, `failure`.
- Fused candidate: the search-hit fields plus `sources`, `fused_score`, and `fused_rank`.
- Content row: `ref`, `text`, `url`, `title`, `date`, `locator`, `locator_error`, `failure`,
  `metadata`.
- Grep report: `matches`, `failures`, `input_count`. A match includes `ref`, `docid`, `url`,
  `title`, `line`, `text`, `before`, `after`, `locator`, `locator_error`, and `input_index`.
- Passage report: `query`, `passages`, `failures`, `input_count`, `unique_ref_count`. A passage
  includes `ref`, source metadata, exact `text`, `coordinates`, `rank`, `score`, `ranker`,
  `locator`, and `locator_error`.
- Coordinates: `start_line`, `start_character`, `end_line`, `end_character`. Lines are 1-indexed;
  characters are 0-indexed and the end position is exclusive.
- Failure: `code`, `message`, `retryable`, `attempts`, `provider_status`,
  `retry_after_seconds`. Content failures also carry `input_index` and `ref`.
- Extraction row: `index`, `data`, `error`, `attempts`; an error has `code`, `message`, and
  `retryable`.
- Evidence locator: `id`, `ref`, `kind`.

There is no public SDK model hierarchy or `types` module. Join capability results by `ref`.

## Failure and alignment semantics

- Catch `BrokerError` for a capability-wide or infrastructure failure. Inspect `code`,
  `retryable`, and `attempts`; attempts may be absent for a transport failure.
- Inspect `batch.failure` for per-query failure. A failed batch has no hits.
- Inspect `row.failure` for per-ref failure. `read` returns one row per input ref in the
  same order.
- `content.passages` exactly deduplicates refs in first-seen order, ranks successful documents
  together, and reports failed fetches in `report.failures`. Empty refs and zero
  passages are successful reports.
- Use `grep_report` when coverage matters. Its `failures` are aligned by
  `input_index`; plain `grep` omits partial failures.
- Treat empty search hits and zero grep matches as success, not failure.
- Inspect each extraction row's `.data` or `.error`; exactly one is present. The result list aligns with
  the input items.
- Let host policy own retries, rate limits, deduplication, and in-flight coalescing. A returned
  failure is final for that call.

## Retrieval and evidence limits

- A session reaches one configured search backend. `domains` works only when that backend supports
  domain filtering.
- Search `offset` is depth into the full ranking. A document is readable only after a search in
  the current session returned its ref, docid, or URL.
- Deployment limits are configurable. Defaults admit at most 64 queries in one search batch and
  256 refs in one content request. Use smaller batches instead of depending on the maxima.
- `content.passages` requires a non-empty query, accepts `limit=1..100` and
  `max_per_ref=1..10`, and applies the per-ref cap after global ranking.
- `grep_report` fetches documents before matching them. Session caching can avoid another backend
  fetch, but every requested ref still counts as a content fetch for strategy budgets.
- `grep` match lines and `read` offsets are 1-indexed. `read.metadata` reports `start_line`,
  `end_line`, `total_lines`, and `next_offset`.
- A non-empty passage no longer than the configured evidence limit, 16,000 characters by default,
  receives a locator unless the evidence registry is full. In that case `locator` is `None` and
  `locator_error.code == "evidence_capacity_exhausted"`.
- Never cite a missing locator. Explicit `locator: None` is invalid. A ref-only citation resolves
  search-preview evidence and must not support a document-content claim.

## Workspace state, output, and lifecycle

- `sdk.state` is the structured interface to the session workspace, not a separate database. There
  is no `sdk.workspace` resource.
- State paths are workspace-relative and cannot escape it. `sdk.state.list(prefix)` returns only
  program artifacts and hides `.opensac-*` runtime files.
- Execution observations show artifact paths, not their contents. A later program must call
  `read_json` or `read_jsonl` to recover saved decisions.
- Use one task-derived namespace such as `runs/<research_id>/`. A conversation can contain more
  than one research task while reusing the same session.
- Use `merge_jsonl` for upserts, then `write_jsonl` when pruning a pool to a fixed bound.
- Persist a constraint fingerprint with each evidence row and attempted refs per constraint.
- Serialize locators with `dict(locator)`. They remain valid only in the live
  session that issued them.
- `sdk.session.usage()` returns `exec_calls`, `search_calls`, `content_fetches`, `llm_calls`,
  `pipeline_model_tokens`, `documents_seen`, `budget_remaining`, and `terminal_reason`.
- Only stdout, stderr, and `sdk.output.submit` return to the control model. They share roughly
  32,000 visible characters, with stdout considered first; reserve space for submitted output.
- On `state_lost`, the failed program was not replayed and the next call starts a clean session.
  Discard every workspace path, ref, and locator from the lost session.
- Adapter observations such as `[sac_run] OpenSAC request failed` and tool-level timeouts occur
  outside the program and are not `BrokerError`. The model-visible adapter surface does not accept
  an execution ID, so a failed observation can have an unknown execution outcome. Inspect
  persisted progress in a new recovery stage instead of replaying the same program.

## Sandbox constraints

Use ordinary Python for deterministic orchestration and `opensac_sdk` only for external
capabilities. Network/process modules and dynamic execution helpers are blocked. Dunder attribute
access is rejected except for `__name__` and `__doc__`; report exceptions with
`type(exc).__name__` rather than `exc.__class__`.
