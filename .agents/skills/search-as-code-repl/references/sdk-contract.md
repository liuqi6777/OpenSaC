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
) -> list[record] | None
sdk.search.many(
    queries, *, limit=10, offset=0, concurrency=5, include_domains=None
) -> list[list[record] | None]
sdk.search.fuse_rrf(
    queries, results, *, weights=None, k=60, limit=None, exclude_domains=None,
    domain_weights=None, max_per_domain=None
) -> list[record]
```

`fuse_rrf` is deterministic local Python and makes no RPC. Its provenance uses `input_index`.

Content:

```python
sdk.content.fetch(source) -> record | None
sdk.content.fetch_many(sources, *, concurrency=5) -> list[record | None]
sdk.content.read(
    source, *, start_line=1, start_character=0,
    line_count=200, max_chars=100_000
) -> record | None
sdk.content.grep(
    pattern, *, sources, mode="regex", case_sensitive=False,
    start_line=1, context_lines=0, limit_per_source=20
) -> list[record | None]
```

Capabilities and workspace:

```python
sdk.capabilities() -> record | None
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
not raise operational failures; branch on `result is None`.

## Exact result fields

- Search hit: `source`, `backend`, `title`, `domain`, `date`, `snippet`, `score`, `rank`,
  `retrieval`, and `metadata`.
- Search result list: one input-aligned position per query. A successful position is the query's
  search-hit list; a failed position is `None`. Keep the original query list for identity.
- Fused candidate: the search-hit fields plus `provenance`, `raw_fused_score`, `domain_weight`,
  `fused_score`, and `fused_rank`. Each provenance row has `input_index`, `query`, `backend`,
  `rank`, and `score`.
- Fetched document: `source`, `text`, `title`, `date`, and provider `metadata`.
- Fetch result list: one input-aligned position per source. A successful position is the fetched
  document; a failed position is `None`. Keep the original source list for identity.
- Read slice: the fetched-document fields plus an independent `window` record. `window` contains
  `start_line`, `start_character`, `end_line`, `end_character`, `total_lines`, `next`, and
  `truncated_by_max_chars`.
- Grep result list: one input-aligned position per source. A successful record contains `source`,
  `title`, `matches`, and `next_start_line`; a failed position is `None`.
- Grep match: `line`, `text`, `before`, `after`, and `spans`. Its source and title come from the
  owning result. Each span has 0-based, end-exclusive `start_character` and `end_character`.
- Coordinates use 1-based lines and 0-based, end-exclusive character positions.
Join capability results by `source`.

## Failure and continuation semantics

- Provider, quota, deadline, transport, protocol, contract, and permission failures return `None`.
  OpenSAC records bounded structured warnings with sanitized operational details; do not parse the
  rendered warning as a program data contract.
- Local argument type, minimum-boundary, and strict-JSON errors raise `ValueError`. Configurable
  upper bounds are broker policy and are discoverable through a non-`None` `sdk.capabilities()`
  result.
- Single operations return `T | None`. Fan-out operations preserve input order, duplicates, and
  partial success as aligned `list[T | None]`; even all-systemic failures remain aligned `None`.
- Check `is None`, never truthiness, because an empty list, string, or object can be successful.
  Use `zip(inputs, results, strict=True)` when failed-item identity matters. Do not add `try/except`
  or print errors merely for visibility.
- `read.window.next` is either `None` at EOF or the exact `start_line`/`start_character` for an
  unlossy follow-up call. This matters when `max_chars` stops within one long line.
- For capped grep scans, continue each successful result from non-null `result.next_start_line`.
  A null cursor means that source was scanned to EOF.
- Empty search hits or grep matches in non-`None` results are successful.
- Let host policy own provider retries, rate limits, caching, and in-flight coalescing.

## Retrieval, quota, and content boundaries

- A session reaches one configured search backend. `include_domains` is accepted only when that
  backend reports support for it.
- Search `offset` is depth into the full ranking. Local document IDs are readable only after search
  returned them; supported web deployments may also admit bounded public HTTP(S) URLs directly.
- Every source requested through `fetch`, `read`, or `grep` consumes public
  content-fetch budget even when session caching avoids backend work.
- A non-`None` `sdk.capabilities()` result reports contract versions, active mechanisms, backend
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
- Adapter failures occur outside the sandbox and are not SDK results; their execution result may be
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
