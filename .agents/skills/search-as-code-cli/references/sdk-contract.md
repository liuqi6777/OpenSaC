# OpenSAC SDK contract used by this skill

Use this reference when exact signatures, fields, limits, or failure semantics matter. It documents
the SDK subset used by this skill, not every SDK capability. Import only `sdk` from `opensac_sdk`.
Structured results are mapping-backed records. Mapping access is canonical; known
non-colliding fields also support `row.source`, and `dict(row)` serializes the record. Use key access
for fields such as `items`, `values`, or `get`.

## Capability surface

Search:

```python
sdk.search(
    query, *, limit=10, offset=0, include_domains=None
) -> outcome[list[record]]
sdk.search.many(
    queries, *, limit=10, offset=0, concurrency=5, include_domains=None
) -> list[outcome[list[record]]]
sdk.search.fuse_rrf(
    report, *, weights=None, k=60, limit=None, exclude_domains=None,
    domain_weights=None, max_per_domain=None
) -> list[record]
```

`fuse_rrf` is deterministic local Python and makes no RPC. Its provenance uses `input_index`.

Content:

```python
sdk.content.fetch(source) -> outcome[record]
sdk.content.fetch_many(sources, *, concurrency=5) -> list[outcome[record]]
sdk.content.read(
    source, *, start_line=1, start_character=0,
    line_count=200, max_chars=100_000
) -> outcome[record]
sdk.content.grep(
    pattern, *, sources, mode="regex", case_sensitive=False,
    start_line=1, context_lines=0, limit_per_source=20
) -> list[outcome[record]]
```

Capabilities and workspace:

```python
sdk.capabilities() -> outcome[record]
sdk.workspace.write_json(path, value)
sdk.workspace.write_jsonl(path, rows)
sdk.workspace.append_jsonl(path, rows)
sdk.workspace.upsert_jsonl(path, rows, key="source") -> int
sdk.workspace.read_json(path)
sdk.workspace.read_jsonl(path)
sdk.workspace.exists(path) -> bool
sdk.workspace.list(prefix="") -> list[str]
```

`search.many` and `content.fetch_many` are public aligned fan-out helpers. Broker-backed methods do
not raise operational failures; branch on their returned outcomes.

## Exact result fields

- Search hit: `source`, `backend`, `title`, `domain`, `date`, `snippet`, `score`, `rank`,
  `retrieval`, and `metadata`.
- Generic outcome: `status` is exactly `"success"` or `"failure"`. Successful outcomes have the
  operation result in `value` and `error=None`; failures have `value=None` and structured `error`.
- Search outcome list: one input-aligned row per query. Each row adds `input_index` and `query` to
  the generic outcome; a successful `value` is the query's search-hit list.
- Fused candidate: the search-hit fields plus `provenance`, `raw_fused_score`, `domain_weight`,
  `fused_score`, and `fused_rank`. Each provenance row has `input_index`, `query`, `backend`,
  `rank`, and `score`.
- Fetched document: `source`, `text`, `title`, `date`, and provider `metadata`.
- Fetch outcome list: one input-aligned row per source. Each row adds `input_index` and `source` to
  the generic outcome; a successful `value` is the fetched document.
- Read slice: the fetched-document fields plus an independent `window` record. `window` contains
  `start_line`, `start_character`, `end_line`, `end_character`, `total_lines`, `next`, and
  `truncated_by_max_chars`.
- Grep outcome list: one input-aligned generic outcome per source with outer `input_index` and
  `source`. A successful `value` contains `source`, `title`, `matches`, and `next_start_line`.
- Grep match: `line`, `text`, `before`, `after`, and `spans`. Its source and title come from the
  owning outcome. Each span has 0-based, end-exclusive `start_character` and `end_character`.
- Coordinates use 1-based lines and 0-based, end-exclusive character positions.
- Outcome error: `code`, `message`, `retryable`, `attempts`, `provider_status`,
  `retry_after_seconds`, `provider`, `component`, and `scope`. Read these fields from
  `outcome.error`; never display or parse search `status` as failure detail.
Join capability results by `source`.

## Failure and continuation semantics

- Provider, quota, deadline, transport, protocol, contract, and permission failures return failure
  outcomes. Inspect `error.code`, `retryable`, `attempts`, `provider`, `component`, and `scope`.
- Local argument type, minimum-boundary, and strict-JSON errors raise `ValueError`. Configurable
  upper bounds are broker policy and are discoverable through a successful `sdk.capabilities()`
  outcome.
- Single operations return one outcome. Fan-out operations preserve input order, duplicates, and
  partial success as aligned outcome lists; even all-systemic failures remain failure outcomes.
- OpenSAC records outcome failures as bounded warnings that the adapter renders automatically.
  Branch on `status` for dataflow, but do not add `try/except` or print errors merely for visibility.
- `read.value.window.next` is either `None` at EOF or the exact `start_line`/`start_character` for an
  unlossy follow-up call. This matters when `max_chars` stops within one long line.
- For capped grep scans, continue each successful outcome from non-null
  `outcome.value.next_start_line`. A null cursor means that source was scanned to EOF.
- Empty search hits or grep matches with success status are successful results.
- Let host policy own provider retries, rate limits, caching, and in-flight coalescing.

## Retrieval, quota, and content boundaries

- A session reaches one configured search backend. `include_domains` is accepted only when that
  backend reports support for it.
- Search `offset` is depth into the full ranking. Local document IDs are readable only after search
  returned them; supported web deployments may also admit bounded public HTTP(S) URLs directly.
- Every source requested through `fetch`, `read`, or `grep` consumes public
  content-fetch budget even when session caching avoids backend work.
- A successful `sdk.capabilities()` value reports contract versions, active mechanisms, backend
  support, and configured upper limits. Do not hard-code deployment maxima.

## Workspace, stdout, and lifecycle

- `sdk.workspace` is the structured session-workspace interface.
- Artifact paths are workspace-relative and cannot escape it. `sdk.workspace.list(prefix)` hides
  internal runtime files. Applications choose their own artifact layout.
- `upsert_jsonl` replaces whole rows by the chosen key; it does not merge object fields.
- Process-per-call programs lose Python variables between calls. Persistent-interpreter variants
  retain completed assignments until an explicit `state_lost` or `interpreter_lost` error. Files
  and live variables remain independent; `mechanisms.persistence` controls files only.
- On `state_lost` or `interpreter_lost`, the failed program is not replayed. Restore trusted
  workspace data, re-admit local IDs, and reuse public URLs only when their deployment permits it.
- Adapter failures occur outside the sandbox and are not SDK outcomes; their execution result may be
  unknown. Repeat external work only when durable progress proves it is missing.

## Runtime documentation and sandbox constraints

Inspect only the method needed by the next stage:

```python
print(sdk.__doc__)
print(sdk.content.read.__doc__)
```

Reading `__doc__` makes no broker call. `help()` remains blocked. Use ordinary Python for
deterministic orchestration; network/process modules and dynamic execution helpers are blocked.
Dunder access is rejected except for `__name__` and `__doc__`.
