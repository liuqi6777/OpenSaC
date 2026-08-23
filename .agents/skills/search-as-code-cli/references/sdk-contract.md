# OpenSAC SDK contract

Use this reference when exact signatures, fields, limits, or failure semantics matter.
Import only `BrokerError` and `sdk` from `opensac_sdk`. Structured results are ordinary JSON
records: both `row.source` and `row["source"]` read the same field, and `dict(row)` serializes it.

## Contents

- [Capabilities](#capabilities)
- [Exact result fields](#exact-result-fields)
- [Failure and alignment semantics](#failure-and-alignment-semantics)
- [Retrieval and content limits](#retrieval-and-content-limits)
- [Workspace state, output, and lifecycle](#workspace-state-output-and-lifecycle)
- [Runtime documentation](#runtime-documentation)
- [Sandbox constraints](#sandbox-constraints)

## Core and helper capabilities

Search:

```python
sdk.search(query, limit=10, offset=0, domains=None) -> list[record]
sdk.search.many(
    queries, limit_per_query=10, offset=0, concurrency=5, domains=None
) -> list[record]
sdk.search.fuse_rrf(
    batches,
    weights=None,
    k=60,
    limit=None,
    exclude_domains=None,
    domain_weights=None,
    max_per_domain=None,
) -> list[record]
```

`fuse_rrf` is deterministic local Python and makes no RPC. Domain exclusions, weights, and the
per-host cap are applied before `limit`; over-fetch each query when a policy may remove candidates.

Content core:

```python
sdk.content.passages(
    query, sources, limit=20, max_per_source=3
) -> record
sdk.content.grep(
    sources, pattern, mode="regex", case_sensitive=False,
    context=0, max_matches_per_source=20
) -> record
sdk.content.read(
    source, offset=1, limit=200, max_chars=100_000
) -> record
sdk.content.read_many(
    [{"source": source, "offset": 1, "limit": 200, "max_chars": 100_000}]
) -> list[record]
```

Session, state, and output:

```python
sdk.session.usage() -> dict
sdk.session.capabilities() -> dict
sdk.state.write_json(path, value)
sdk.state.write_jsonl(path, rows)
sdk.state.append_jsonl(path, rows)
sdk.state.merge_jsonl(path, rows, key="source") -> int
sdk.state.read_json(path)
sdk.state.read_jsonl(path)
sdk.state.exists(path) -> bool
sdk.state.list(prefix="") -> list[str]
sdk.output.submit(output, citations=[source_url])
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

- Search hit: `source`, `backend`, `title`, `domain`, `date`, `snippet`, `score`,
  `rank`, `retrieval`, `metadata`.
- Search batch: `query`, `hits`, `failure`.
- Fused candidate: the search-hit fields plus `provenance`, `raw_fused_score`, `domain_weight`,
  `fused_score`, and `fused_rank`.
- Content row: `source`, `text`, `title`, `date`, `failure`, `metadata`.
- Grep report: `pattern`, `mode`, `case_sensitive`, `context`, `max_matches_per_source`, `matches`,
  `source_results`, and `input_count`. A match includes `source`, `title`, `line`, `text`, `before`,
  `after`, and `input_index`. Each input-aligned source result includes `input_index`, `source`,
  `title`, `match_count`, `scan_complete`, and `failure`.
- Passage report: `query`, `passages`, `failures`, `input_count`, `unique_source_count`. A passage
  includes `source`, source metadata, exact `text`, `coordinates`, `rank`, `score`, and `ranker`.
- Coordinates: `start_line`, `start_character`, `end_line`, `end_character`. Lines are 1-indexed;
  characters are 0-indexed and the end position is exclusive.
- Failure: `code`, `message`, `retryable`, `attempts`, `provider_status`,
  `retry_after_seconds`, `provider`, `operation`, and `scope`. Scope is `request`, `resource`,
  `provider`, or `unknown`; content failures also carry `input_index` and `source`.
- Extraction row: `index`, `data`, `failure`, `attempts`; a failure has `code`, `message`, and
  `retryable`.

There is no public SDK model hierarchy or `types` module. Join capability results by `source`.

## Failure and alignment semantics

- Catch `BrokerError` for a capability-wide or infrastructure failure. Inspect `code`,
  `retryable`, `attempts`, `provider`, `operation`, and `scope`; nullable fields may be absent for
  a broker transport failure. Treat `unknown` as deliberately unclassified, not provider-wide.
- Inspect `batch.failure` for per-query failure. A failed batch has no hits.
- Inspect `row.failure` for a single-source `read` failure. `read_many` returns one row per input
  window in the same order and includes `input_index`.
- `content.passages` exactly deduplicates sources in first-seen order, ranks successful documents
  together, and reports failed fetches in `report.failures`. Empty sources and zero
  passages are successful reports.
- Use `grep` when coverage matters. Its `source_results` align by `input_index`; inspect each
  row's `failure` and `scan_complete` to distinguish failed, capped, and complete scans.
- Treat empty search hits and zero grep matches as success, not failure.
- Inspect each extraction row's `.data` or `.failure`; exactly one is present. The result list
  aligns with the input items.
- Let host policy own retries, rate limits, deduplication, and in-flight coalescing. A returned
  failure is final for that call.

## Retrieval and content limits

- A session reaches one configured search backend. `domains` works only when that backend supports
  domain filtering.
- Search `offset` is depth into the full ranking. Local document IDs are readable only after search
  returned them; web deployments also accept bounded public HTTP(S) URLs directly.
- Deployment limits are configurable. Defaults admit at most 64 queries in one search batch and
  256 sources in one content request. Use smaller batches instead of depending on the maxima.
- `content.passages` requires a non-empty query, accepts `limit=1..100` and
  `max_per_source=1..10`, and applies the per-source cap after global ranking.
- `grep` fetches documents before matching them. Session caching can avoid another backend
  fetch, but every requested source still counts as a content fetch for strategy budgets.
- `grep` match lines and `read` offsets are 1-indexed. `read.metadata` reports `start_line`,
  `end_line`, `total_lines`, and `next_offset`.
- Content accepts only URL/local-ID strings, not search-hit or content-result records.
- `citations` is an optional list of at most 256 non-empty source strings. It is written locally,
  does not call the broker, and is not evidence validation.

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
- Persist a constraint fingerprint with each evidence row and attempted sources per constraint.
- `sdk.session.usage()` returns call counters, `content_backend_fetches`, token reservations,
  sandbox/workspace usage, `budget_consumed`, `budget_remaining`, provider metrics, and
  `terminal_reason`. Use `sdk.session.capabilities()` to discover the active contracts, limits,
  methods, backends, and optional mechanisms instead of hard-coding deployment assumptions.
- Only stdout, stderr, and `sdk.output.submit` return to the control model. They share roughly
  32,000 visible characters, with stdout considered first; reserve space for submitted output.
- On `state_lost`, the failed program was not replayed and the next call starts a clean session.
  Rebuild workspace state and local-ID admission; public web URLs remain reusable.
- Adapter observations such as `[sac_run] OpenSAC request failed` and tool-level timeouts occur
  outside the program and are not `BrokerError`. The model-visible adapter surface does not accept
  an execution ID, so a failed observation can have an unknown execution outcome. Inspect
  persisted progress in a new recovery stage instead of replaying the same program.

## Runtime documentation

The installed SDK carries bounded documentation on every agent-facing resource and operation.
Inspect only the interface needed by the next stage:

```python
print(sdk.__doc__)                       # namespace overview
print(sdk.search.__doc__)                # resource purpose and boundary
print(sdk.content.passages.__doc__)      # exact operation semantics
```

Reading `__doc__` does not execute a capability or call the broker. `help()` remains blocked,
and printing many docstrings can consume the observation budget; prefer one exact method at a time.

## Sandbox constraints

Use ordinary Python for deterministic orchestration and `opensac_sdk` only for external
capabilities. Network/process modules and dynamic execution helpers are blocked. Dunder attribute
access is rejected except for `__name__` and `__doc__`; report exceptions with
`type(exc).__name__` rather than `exc.__class__`.
