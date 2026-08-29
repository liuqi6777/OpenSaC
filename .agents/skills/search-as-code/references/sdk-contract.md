# OpenSAC SDK contract

Use this reference when exact signatures, fields, limits, or failure semantics matter. Import only
`BrokerError` and `sdk` from `opensac_sdk`. Structured results are mapping-backed records. Mapping
access is canonical; known non-colliding fields also support `row.source`, and `dict(row)`
serializes the record. Use key access for fields such as `items`, `values`, or `get`.

## Capability surface

Search:

```python
sdk.search(
    query, *, limit=10, offset=0, include_domains=None
) -> list[record]
sdk.search.many(
    queries, *, limit=10, offset=0, concurrency=5, include_domains=None
) -> list[record]
sdk.search.fuse_rrf(
    report, *, weights=None, k=60, limit=None, exclude_domains=None,
    domain_weights=None, max_per_domain=None
) -> list[record]
```

`fuse_rrf` is deterministic local Python and makes no RPC. Its provenance uses `input_index`.

Content:

```python
sdk.content.fetch(source) -> record
sdk.content.read(
    source, *, start_line=1, start_character=0,
    line_count=200, max_chars=100_000
) -> record
sdk.content.grep(
    pattern, *, sources, mode="regex", case_sensitive=False,
    start_line=1, context_lines=0, limit_per_source=20
) -> list[record]
sdk.content.passages(
    query, *, sources, limit=20, limit_per_source=3
) -> record
```

LLM, session, state, and output:

```python
sdk.llm.extract(
    item, *, instruction, schema, max_tokens=None, repair_attempts=0
) -> dict

sdk.session.usage() -> record
sdk.session.capabilities() -> record
sdk.state.write_json(path, value)
sdk.state.write_jsonl(path, rows)
sdk.state.append_jsonl(path, rows)
sdk.state.upsert_jsonl(path, rows, key="source") -> int
sdk.state.read_json(path)
sdk.state.read_jsonl(path)
sdk.state.exists(path) -> bool
sdk.state.list(prefix="") -> list[str]
sdk.output.submit(value, citations=[source_url])
```

Only `search.many` is a public batch helper. Loop over independent fetches, reads, completions, or
extractions in ordinary Python and handle each `BrokerError` at the point where it occurs.

## Exact result fields

- Search hit: `source`, `backend`, `title`, `domain`, `date`, `snippet`, `score`, `rank`,
  `retrieval`, and `metadata`.
- Search outcome list: one input-aligned row per query with `query`, `status`, `hits`, and `error`.
  List position is the input identity. Status is exactly `"success"` or `"failure"`; successful
  rows have `error=None`, while failed rows have empty `hits` and a structured `error`.
- Fused candidate: the search-hit fields plus `provenance`, `raw_fused_score`, `domain_weight`,
  `fused_score`, and `fused_rank`. Each provenance row has `input_index`, `query`, `backend`,
  `rank`, and `score`.
- Fetched document: `source`, `text`, `title`, `date`, and provider `metadata`.
- Read slice: the fetched-document fields plus an independent `window` record. `window` contains
  `start_line`, `start_character`, `end_line`, `end_character`, `total_lines`, `next`, and
  `truncated_by_max_chars`.
- Grep outcome list: one input-aligned row per source with `source`, `title`, `status`, `matches`,
  and `next_start_line`. Failed rows have `title=None`, empty `matches`, and no continuation.
- Grep match: `line`, `text`, `before`, `after`, and `spans`. Its source and title come from the
  owning outcome. Each span has 0-based, end-exclusive `start_character` and `end_character`.
- Passage report: `query`, `passages`, `failures`, `warnings`, `input_count`, and
  `unique_source_count`. A passage contains source metadata, exact `text`, `coordinates`, `rank`,
  `score`, and `ranker`.
- Coordinates use 1-based lines and 0-based, end-exclusive character positions.
- Search outcome error: `code`, `message`, `retryable`, `attempts`, `provider_status`,
  `retry_after_seconds`, `provider`, `component`, and `scope`. Read these fields from
  `outcome.error`; never display or parse search `status` as failure detail.
- Grep outcome status is exactly `"success"` or a bounded human-readable failure string. Only
  compare it with `"success"`; do not parse failure text.
- Passage failure: `code`, `message`, `retryable`, `attempts`, `provider_status`,
  `retry_after_seconds`, `provider`, `component`, `scope`, `input_index`, and `source`.
- `llm.extract` returns the schema-validated JSON object directly.

Mapping access is canonical: use `row["field"]`, `get`, `keys`, `items`, `values`, iteration, or
`dict(row)`. Attribute access is only a convenience for known, non-colliding fields; access keys
such as `items`, `values`, or `get` with brackets.

There is no public SDK model hierarchy or `types` module. Join capability results by `source`.

## Failure and continuation semantics

- Catch `BrokerError` for provider, quota, transport, JSON-output, schema-validation, and repair
  failures. Inspect `code`, `retryable`, `attempts`, `provider`, `component`, and `scope`.
- Local argument type, minimum-boundary, and strict-JSON errors raise `ValueError`. Configurable
  upper bounds are broker policy and are discoverable through `sdk.session.capabilities()`.
- `search.many` preserves partial success as input-aligned outcomes. Branch on
  `status == "success"`; failed rows use `outcome.error`. Provider, quota, and deadline errors stay
  item failures, while an all-systemic transport/protocol/contract/permission failure can raise one
  representative top-level `BrokerError`.
- `content.grep` preserves partial success as input-aligned outcomes; other statuses are displayable
  failure text. `content.passages` retains structured fetch failures beside successful passages.
- `content.fetch`, `content.read`, `llm.complete`, and `llm.extract` are single operations. A
  failure is a top-level `BrokerError`; Python loops decide whether to continue with later items.
- `read.window.next` is either `None` at EOF or the exact `start_line`/`start_character` for an
  unlossy follow-up call. This matters when `max_chars` stops within one long line.
- For capped grep scans, continue each successful outcome from non-null `next_start_line`.
  `next_start_line=None` means that successful source was scanned to EOF.
- Empty search hits or grep matches with success status, and zero passages without a structured
  failure, are successful results.
- `content.passages` deduplicates sources in first-seen order. A failed configured reranker falls
  back to `lexical:bm25` and appears in `warnings`.
- Let host policy own provider retries, rate limits, caching, and in-flight coalescing.

## Retrieval, quota, and content boundaries

- A session reaches one configured search backend. `include_domains` is accepted only when that
  backend reports support for it.
- Search `offset` is depth into the full ranking. Local document IDs are readable only after search
  returned them; supported web deployments may also admit bounded public HTTP(S) URLs directly.
- Pass source strings, not search-hit or content-result records, to content methods.
- Every source requested through `fetch`, `read`, `grep`, or `passages` consumes public
  content-fetch budget even when session caching avoids backend work.
- Search results are a candidate pool, not a content batch. Promote only a small, high-relevance,
  source-diverse subset whose metadata suggests it can close a current evidence gap; do not fetch an
  entire result list or fused pool. Expand incrementally after inspecting the current subset.
- For each promoted source, call `fetch` once before any other content method. Reuse the returned text
  for exact matching, regexes, slicing, and multiple checks in local Python. When later programs will
  reuse the full text, optionally persist one copy with `sdk.state`; full-document artifacts consume
  workspace budget.
- `passages` is the distinct semantic-localization option: after fetch, use it when relevance cannot
  be captured reliably by lexical checks or when passages must be ranked across long or multiple
  selected documents. It can complement local inspection and is not mandatory. Scope each passage
  query to the fetched sources plausibly relevant to that question instead of crossing every query
  with the whole selected set.
- `grep` and `read` are usually replaceable by local Python after fetch. Do not call them just to
  rediscover or reformat a match already available in fetched text; call them only when a provider
  window or cursor is itself useful.
- `passages`, `grep`, and `read` accept source strings and may reuse the session cache, avoiding
  backend retrieval, but every requested source remains another logical content-fetch charge. Never
  pass an unfetched source to them, and never print or submit a complete fetched document.
- Treat snippets as triage. Inspect fetched, passage, grep, or read text for material claims.
- `sdk.session.usage()` exposes only `exec_calls`, `search_calls`, `content_fetches`, `llm_calls`,
  `pipeline_output_tokens_reserved`, `sandbox_seconds`, `workspace_bytes`, `budget_remaining`, and
  `terminal_reason`. `None` in `budget_remaining` means that budget is unlimited.
- Resource budgets are enforced by the broker. Every initial or repair model attempt reserves one
  LLM call before dispatch.
- `sdk.session.capabilities()` reports contract versions, active mechanisms, backend support, and
  configured upper limits. Do not hard-code deployment maxima.

## State, output, and lifecycle

- `sdk.state` is the structured session-workspace interface; there is no `sdk.workspace` resource.
- State paths are workspace-relative and cannot escape it. `sdk.state.list(prefix)` hides internal
  runtime files. The namespace shape is application state, not an SDK requirement.
- `upsert_jsonl` replaces whole rows by the chosen key; it does not merge object fields.
- `citations` is an optional list of source strings. Submission records labels but does not validate
  evidence.
- `sdk.output.submit` atomically replaces the current execution's structured output artifact. It
  does not call the broker, terminate the program, or complete the agent task.
- Process-per-call programs lose Python variables between calls. Persistent-interpreter variants
  retain completed assignments only while the observation reports `interpreter_state=ready`.
  Files and live variables remain independent; `mechanisms.persistence` controls files only.
- On `state_lost` or `interpreter_state=lost`, the failed program is not replayed. Restore trusted
  state, re-admit local IDs, and reuse public URLs only when their deployment permits it. A direct
  persistent session may surface this terminal state as `interpreter_lost`.
- Adapter failures occur outside the sandbox and are not `BrokerError`; their execution outcome may
  be unknown. Inspect durable progress and usage before repeating external work.

## Runtime documentation and sandbox constraints

Inspect only the method needed by the next stage:

```python
print(sdk.__doc__)
print(sdk.content.read.__doc__)
```

Reading `__doc__` makes no broker call. `help()` remains blocked. Use ordinary Python for
deterministic orchestration; network/process modules and dynamic execution helpers are blocked.
Dunder access is rejected except for `__name__` and `__doc__`.
