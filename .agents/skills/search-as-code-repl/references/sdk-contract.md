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
) -> record
sdk.search.fuse_rrf(
    report,
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
) -> record
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
) -> record
```

The `extract_many` schema must be a JSON-serializable object whose root type is `object`.
`repair_attempts` accepts only `0` or `1`. A missing pipeline-model configuration raises a broker
error; keep a deterministic fallback.

## Exact result fields

- Search hit: `source`, `backend`, `title`, `domain`, `date`, `snippet`, `score`,
  `rank`, `retrieval`, `metadata`.
- Search report: `results`, `failures`, `input_count`. A successful result has `input_index`,
  `query`, and `hits`; a failure has `input_index`, `query`, and the failure fields below.
- Fused candidate: the search-hit fields plus `provenance`, `raw_fused_score`, `domain_weight`,
  `fused_score`, and `fused_rank`.
- Content row: `source`, `text`, `title`, `date`, and `metadata`.
- Content batch report: `results`, `failures`, `input_count`. Both outcome lists carry
  `input_index`; failures also carry `source`.
- Grep report: `pattern`, `mode`, `case_sensitive`, `context`, `max_matches_per_source`, `matches`,
  `source_results`, `failures`, and `input_count`. A match includes `source`, `title`, `line`,
  `text`, `before`, `after`, and `input_index`. Each successful source result includes
  `input_index`, `source`, `title`, `match_count`, and `scan_complete`.
- Passage report: `query`, `passages`, `failures`, `warnings`, `input_count`,
  `unique_source_count`. A passage includes `source`, source metadata, exact `text`, `coordinates`,
  `rank`, `score`, and `ranker`.
- Coordinates: `start_line`, `start_character`, `end_line`, `end_character`. Lines are 1-indexed;
  characters are 0-indexed and the end position is exclusive.
- Failure: `code`, `message`, `retryable`, `attempts`, `provider_status`,
  `retry_after_seconds`, `provider`, `component`, and `scope`. Scope is `request`, `resource`,
  `provider`, or `unknown`; content failures also carry `input_index` and `source`.
- Extraction report: `results`, `failures`, `input_count`. Successful rows have `input_index`,
  `data`, and `attempts`; failed rows have `input_index`, `attempts`, and the failure fields.

There is no public SDK model hierarchy or `types` module. Join capability results by `source`.

## Failure and alignment semantics

- `sac_run` automatically renders external item-failure warnings before stdout. Partial and
  complete item failures preserve input identity through `input_index`; inspect report failures
  only when code must branch on them.
- Catch `BrokerError` for a request/infrastructure failure that cannot return a safe documented
  result shape. Inspect `code`,
  `retryable`, `attempts`, `provider`, `component`, and `scope`; nullable fields may be absent for
  a broker transport failure. Treat `unknown` as deliberately unclassified, not provider-wide.
- Multi-item operations keep successful rows in `report.results` and failed rows in
  `report.failures`; together their `input_index` values partition the original inputs.
- A failed single-source `read` raises `BrokerError`. `read_many` returns a partitioned report so
  one unreadable source does not hide successful windows.
- `content.passages` exactly deduplicates sources in first-seen order, ranks successful documents
  together, and reports failed fetches in `report.failures`. A failed configured reranker falls
  back to `lexical:bm25` and appears in `report.warnings`. Empty sources and zero passages without
  typed failures are successful reports.
- Use `grep` when coverage matters. Inspect successful `source_results` and separate `failures` by
  `input_index`; `scan_complete` distinguishes capped and complete successful scans.
- Treat empty search hits and zero grep matches without a typed failure as success, not failure.
- Inspect `extract_many` results and failures separately; their `input_index` values partition the
  input items.
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
- Each grep match has one string `text` line and `before` / `after` context as `list[str]`. Use its
  `source` and 1-indexed `line` to select a focused `read` window for material evidence instead of
  assuming the context fields are one string.
- `grep` match lines and `read` offsets are 1-indexed. `read.metadata` reports `start_line`,
  `end_line`, `total_lines`, and `next_offset`.
- Content accepts only URL/local-ID strings, not search-hit or content-result records.
- `citations` is an optional list of at most 256 non-empty source strings. It is written locally,
  does not call the broker, and is not evidence validation.
- `sdk.output.submit` atomically writes the current execution's structured output artifact. It does
  not call the broker, terminate the program, complete the agent task, or validate the answer.
- Repeated submissions in one execution replace the prior artifact. A well-formed research program
  should submit at most once, only when its caller needs a structured runtime result.

## Workspace state, output, and lifecycle

- In `persistent_interpreter` mode, one internal `default` interpreter belongs to the current
  OpenSAC session. Top-level variables, functions, imports, and assignments completed before an
  ordinary exception remain available to later cells.
- The first adapter observation must report `execution_mode=persistent_interpreter`. Continue only
  while `interpreter_state=ready`; `not_started` is possible before the first successful cell.
- `sdk.state` is the structured interface to the session workspace, not a separate database. There
  is no `sdk.workspace` resource.
- State paths are workspace-relative and cannot escape it. `sdk.state.list(prefix)` returns only
  program artifacts and hides `.opensac-*` runtime files.
- Execution observations show artifact paths, not their contents. A later program must call
  `read_json` or `read_jsonl` to recover saved decisions.
- Choose workspace-relative namespaces that avoid collisions when a conversation contains multiple
  research tasks. The namespace shape is application state, not an SDK requirement.
- `merge_jsonl` upserts rows by a chosen key, while `write_jsonl` replaces the artifact. Choose either
  operation from the state model the program needs.
- `sdk.session.usage()` returns call counters, `content_backend_fetches`, token reservations,
  sandbox/workspace usage, `budget_consumed`, `budget_remaining`, provider metrics, and
  `terminal_reason`. Use `sdk.session.capabilities()` to discover the active contracts, limits,
  methods, backends, and optional mechanisms instead of hard-coding deployment assumptions.
- Structured external-failure warnings, stdout, stderr, and `sdk.output.submit` return to the
  control model. They share roughly 32,000 visible characters; warnings use a bounded leading
  section, then stdout is considered first. Reserve space for submitted output.
- `mechanisms.persistence` controls files only. With persistence disabled, each cell receives a new
  temporary workspace even though Python globals remain alive. The SDK resolves the active
  workspace, execution ID, output path, and broker token at call time. Check the mechanism through
  `sdk.session.capabilities()` before treating `sdk.state` as cross-cell recovery state.
- On `interpreter_state=lost` or `state_lost`, the failed cell is not replayed. The direct session
  returns `410 interpreter_lost` on later execution; the agent adapter discards it and binds the
  next invocation to a clean session. Restore a trustworthy checkpoint if one exists, rebuild
  local-ID admission, and reuse public web URLs when appropriate.
- Adapter observations such as `[sac_run] OpenSAC request failed` and tool-level timeouts occur
  outside the program and are not `BrokerError`. The model-visible adapter surface does not accept
  an execution ID, so a failed observation can have an unknown execution outcome. Inspect relevant
  globals and `sdk.session.usage()` in a small read-only recovery cell before retrying anything.

## Runtime documentation

The installed SDK carries bounded documentation on every agent-facing resource and operation.
Inspect only the interface needed by the next stage:

```python
print(sdk.__doc__)  # namespace overview
print(sdk.search.__doc__)  # resource purpose and boundary
print(sdk.content.passages.__doc__)  # exact operation semantics
```

Reading `__doc__` does not execute a capability or call the broker. `help()` remains blocked,
and printing many docstrings can consume the observation budget; prefer one exact method at a time.

## Sandbox constraints

Use ordinary Python for deterministic orchestration and `opensac_sdk` only for external
capabilities. Network/process modules and dynamic execution helpers are blocked. Dunder attribute
access is rejected except for `__name__` and `__doc__`; report exceptions with
`type(exc).__name__` rather than `exc.__class__`.
