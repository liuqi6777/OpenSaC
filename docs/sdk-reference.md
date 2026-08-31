# OpenSAC SDK reference

This reference covers the current bundled `opensac_sdk` interface on `main`. Capability contract 15
requires a matching SDK and broker; sandbox contract 14 remains unchanged.

## Conventions

Import the synchronous SDK with:

```python
from opensac_sdk import sdk
```

Public object results are mapping-backed records. Mapping access is canonical; known non-colliding
fields also support convenient attribute reads:

```python
source = row.source
assert source == row["source"]
plain = dict(row)
```

Use `row["items"]`, `row["values"]`, or `row["get"]` when a JSON field collides with a dict method.

The SDK validates types, strict JSON, and basic lower bounds. Deployment-specific upper bounds are
enforced by the broker and reported by a successful `sdk.capabilities()` result.

- Local argument errors raise `ValueError`.
- Provider, quota, deadline, transport, protocol, permission, extraction JSON/schema, and repair
  failures return `None` and are recorded as structured warnings.
- `search.many`, `content.fetch_many`, and `llm.extract_many` are public aligned fan-out helpers.
  They return one result or `None` per input position.

Every broker-backed unary method returns `T | None`; aligned fan-out returns `list[T | None]`.
Use identity checks:

```python
hits = sdk.search("query")
if hits is None:
    print("NEXT: revise the query")
```

Always check `is None`, not truthiness: `[]`, `""`, and `{}` can be successful results. OpenSAC
automatically records bounded failure warnings for agent observation rendering; callers do not need
`try/except` or failure printing for operational errors. Local argument errors and unexpected
programming exceptions still propagate.

## Public surface

| Namespace | Operations |
| --- | --- |
| Search | `search`, `search.many`, `search.fuse_rrf` |
| Content | `content.fetch`, `content.fetch_many`, `content.read`, `content.grep`, `content.passages` |
| LLM | `llm.complete`, `llm.extract`, `llm.extract_many` |
| Top level | `capabilities` |
| Workspace | JSON/JSONL operations including `workspace.upsert_jsonl` |

## Search

### `sdk.search(...)`

```python
sdk.search(
    query: str,
    *,
    limit: int = 10,
    offset: int = 0,
    include_domains: list[str] | None = None,
) -> list[Record] | None
```

`offset` is ranking depth, not a page number. `include_domains` is accepted only when the active
backend reports support for domain filtering.

The successful result is the ranked hit list. Each hit contains
`source`, `backend`, `title`, `domain`, `date`, `snippet`, `score`, `rank`, `retrieval`, and
`metadata`. An empty list is a valid no-match result; `None` means an operational failure.

### `sdk.search.many(...)`

```python
sdk.search.many(
    queries: list[str],
    *,
    limit: int = 10,
    offset: int = 0,
    concurrency: int = 5,
    include_domains: list[str] | None = None,
) -> list[list[Record] | None]
```

The returned list is aligned one-to-one with `queries`. A successful position contains its ranked
hit list and a failed position contains `None`:

```python
queries = ["q1", "q2"]
results = sdk.search.many(queries)
for query, hits in zip(queries, results, strict=True):
    if hits is None:
        continue
    print(query, len(hits))
```

Admission, provider, quota, deadline, transport, protocol, contract, and permission failures remain
aligned as `None`, including when every item fails. Empty input returns `[]` without a broker call.
The structured warning carries `input_index` and query context; the returned list deliberately does
not expose programmatic error details.

`Mechanisms.batching` controls this operation only. When batching is disabled, one query is still
accepted but a wider fan-out is rejected.

The implementation is a single bounded SDK thread-pool path. It checks the deployment manifest
returned by `sdk.capabilities()` for admission and then issues one `search.query` call per input;
there is no environment variable or broker/client mode switch. Its concurrency value is helper
admission, not the provider semaphore.
The broker still owns budgets, rate limits, retries, cache/coalescing, and actual provider
concurrency. The SDK does not deduplicate, and the broker exposes no batch search RPC. The
[release notes](opensac-0.8.2.md) describe the migration boundaries.

### `sdk.search.fuse_rrf(...)`

```python
sdk.search.fuse_rrf(
    queries: list[str],
    results: list[list[Record] | None],
    *,
    weights: list[float] | None = None,
    k: int = 60,
    limit: int | None = None,
    exclude_domains: list[str] | None = None,
    domain_weights: dict[str, float] | None = None,
    max_per_domain: int | None = None,
) -> list[Record]
```

This is deterministic local Python and makes no broker call. Fused rows extend search hits with
`provenance`, `raw_fused_score`, `domain_weight`, `fused_score`, and `fused_rank`. Each provenance
row contains `input_index`, `query`, `backend`, `rank`, and `score`. Queries and results must be
equal-length aligned lists. `None` results are skipped, successful empty lists are accepted, and
`weights` still aligns with every input position.

## Content

Pass source strings to content operations. Local document IDs must have been admitted by search in
the current session. Web deployments may additionally admit bounded public HTTP(S) URLs according
to host policy.

### `sdk.content.fetch(...)`

```python
sdk.content.fetch(source: str) -> Record | None
```

A successful result is one complete normalized document with `source`, `text`, `title`, `date`,
and provider-owned `metadata`. Repeated calls for the same source can reuse the session cache,
although each request still consumes the public content-fetch budget.

### `sdk.content.fetch_many(...)`

```python
sdk.content.fetch_many(
    sources: list[str],
    *,
    concurrency: int = 5,
) -> list[Record | None]
```

This SDK helper makes bounded concurrent calls to unary `content.fetch`. It preserves input order
and duplicate sources, performs no capability-manifest preflight, and returns `[]` for empty input.
`concurrency` bounds only SDK worker fan-out; broker budget, retry, cache, trace, and provider
concurrency policies remain authoritative for each request.

Each input has one aligned result. Preserve the original inputs when failed-source identity matters:

```python
documents = sdk.content.fetch_many(sources)
for source, document in zip(sources, documents, strict=True):
    if document is None:
        continue
    print(source, document.title)
```

Every operational failure remains a `None` position, including all-systemic failures. Unexpected
programming exceptions propagate, and warnings retain bounded per-input context.

### `sdk.content.read(...)`

```python
sdk.content.read(
    source: str,
    *,
    start_line: int = 1,
    start_character: int = 0,
    line_count: int = 200,
    max_chars: int = 100_000,
) -> Record | None
```

Lines are 1-based. Characters are 0-based and end-exclusive. A successful result contains the
document fields plus an independent `window`:

```python
{
    "start_line": int | None,
    "start_character": int,
    "end_line": int | None,
    "end_character": int,
    "total_lines": int,
    "next": {"start_line": int, "start_character": int} | None,
    "truncated_by_max_chars": bool,
}
```

Pass `window.next` back as `start_line` and `start_character` to continue without losing a newline
or characters, including when `max_chars` stops inside one very long line. Reading past EOF returns
empty text and `next=None`; invalid coordinates raise `ValueError`.

### `sdk.content.grep(...)`

```python
sdk.content.grep(
    pattern: str,
    *,
    sources: list[str],
    mode: Literal["regex", "literal"] = "regex",
    case_sensitive: bool = False,
    start_line: int = 1,
    context_lines: int = 0,
    limit_per_source: int = 20,
) -> list[Record | None]
```

The returned list is aligned one-to-one with `sources`. Each successful result contains `source`,
`title`, `matches`, and `next_start_line`; a failed source has `None` in its position:

```python
results = sdk.content.grep("target", sources=sources)
for source, result in zip(sources, results, strict=True):
    if result is None:
        continue
    print(source, len(result.matches))
```

A match contains 1-based `line`, `text`, `before`, `after`, and `spans`. Each span contains 0-based,
end-exclusive `start_character` and `end_character`. On success, continue a capped scan from a
non-null `result.next_start_line`; `None` means that source was scanned to EOF. A successful result
with zero matches is not a failure.

### `sdk.content.passages(...)`

```python
sdk.content.passages(
    query: str,
    *,
    sources: list[str],
    limit: int = 20,
    limit_per_source: int = 3,
) -> Record | None
```

The broker deduplicates sources in first-seen order, ranks passages globally, then applies the
per-source cap. A successful report contains `query`, `passages`, `failures`,
`warnings`, `input_count`, and `unique_source_count`. Passage rows include source metadata, exact
`text`, coordinates, `rank`, `score`, and `ranker`. A reranker failure falls back to lexical BM25
and appears in the report and automatically rendered warnings.

## LLM

Pipeline-model access is optional. Prefer deterministic Python where it is sufficient.

### `sdk.llm.complete(...)`

```python
sdk.llm.complete(
    prompt: str,
    *,
    system: str | None = None,
    temperature: float = 0.2,
    max_tokens: int | None = None,
) -> str | None
```

### `sdk.llm.extract(...)`

```python
sdk.llm.extract(
    item: Any,
    *,
    instruction: str,
    schema: dict[str, Any],
    max_tokens: int | None = None,
    repair_attempts: int = 0,
) -> dict[str, Any] | None
```

`item` and `schema` must be strict-JSON serializable, and the schema root must describe an object.
A successful result is the validated object. `repair_attempts=1` permits one broker-managed
repair. Every initial or repair model attempt reserves quota before dispatch. Invalid provider
output, non-JSON output, schema mismatch, exhausted repair, provider failure, and quota exhaustion
return `None`; bounded structured warnings preserve the relevant operational details.

### `sdk.llm.extract_many(...)`

```python
sdk.llm.extract_many(
    items: list[Any],
    *,
    instruction: str,
    schema: dict[str, Any],
    concurrency: int = 4,
    max_tokens: int | None = None,
    repair_attempts: int = 0,
) -> list[dict[str, Any] | None]
```

Every item shares the instruction, schema, token bound, and repair policy. All items are validated
as strict JSON before fan-out, while original items are omitted from diagnostics.
Each item remains an independent unary `llm.extract` request, so broker quota, retries, tracing,
and provider concurrency remain authoritative.

The returned list is input-aligned. Successful positions contain schema-validated objects; provider,
schema, quota, deadline, and systemic failures remain `None` positions. Warning details include the
input index but intentionally omit the original item.

## Capabilities

### `sdk.capabilities()`

Returns `Record | None`. A successful result contains contract versions, search backend support,
content/LLM upper limits, and active mechanism switches. Generated programs should inspect it
instead of hard-coding deployment maxima.

## Workspace

Artifact paths are workspace-relative and cannot escape the session workspace.

```python
sdk.workspace.write_json(path, value)
sdk.workspace.read_json(path)
sdk.workspace.write_jsonl(path, rows)
sdk.workspace.append_jsonl(path, rows)
sdk.workspace.upsert_jsonl(path, rows, key="source") -> int
sdk.workspace.read_jsonl(path)
sdk.workspace.exists(path) -> bool
sdk.workspace.list(prefix="") -> list[str]
```

`upsert_jsonl` preserves first-seen order and replaces the complete row for an existing key; it is
not a field-level merge.

Return bounded results with Python's `print(...)`, carrying exact source strings beside the evidence
they support. Persist larger structured values with `sdk.workspace` instead of printing full documents
or ledgers.

## 0.8.4 breaking migration

Broker-backed calls no longer expose operational failures as `BrokerError`. There is no compatibility
shim for `Outcome`, its resource-specific variants, or the previous `fuse_rrf(report, ...)` shape.

| Previous return | Current return |
| --- | --- |
| unary `Outcome[T]` | `T | None` |
| aligned `list[Outcome[T]]` | `list[T | None]` |
| `outcome.status` / `.value` / `.error` | `result is None` plus automatic structured warning |
| `fuse_rrf(search_outcomes, ...)` | `fuse_rrf(queries, search_results, ...)` |

Use `is None`, not truthiness. The SDK catches only `BrokerError`; local `ValueError` and unexpected
programming exceptions still propagate. This generated-program API change does not alter capability
contract 15 or sandbox contract 14. See the [current release notes](opensac-0.8.4.md).

## 0.8.3 breaking migration

No aliases or deprecation shims are provided. Host usage accounting, execution records, and dashboard
metrics remain available outside the generated-program SDK.

| 0.8.2 generated-program API | 0.8.3 replacement |
| --- | --- |
| `sdk.session.usage()` | Host REST, storage, or dashboard observability |
| `sdk.session.capabilities()` | `sdk.capabilities()` |
| `sdk.output.submit(...)` | Bounded `print(...)` and `sdk.workspace` artifacts |
| `sdk.state.*` | `sdk.workspace.*` |

The broker rejects `session.usage` under capability contract 15. The workspace and capability
namespace changes add no broker operations. Sandbox contract 14 is unchanged. See the
[v0.8.3 release notes](opensac-0.8.3.md) for the full boundary.

## 0.8.2 breaking migration

No broker batch compatibility handler or SDK mode switch is provided.

| 0.8.1 behavior or API | 0.8.2 replacement |
| --- | --- |
| broker `search.query_many` transport | bounded SDK calls to unary `search.query` |
| failed search outcome status contains display text | `status == "failure"` plus structured `error` |
| broker-facing `BatchSearchBackend` | unary `SearchBackend.search` |
| `LocalSearchBackend.search_many(...)` | concurrent unary adapter calls; backend-internal batching is private |

The `sdk.search.many` signature is unchanged. Deploy the 0.8.2 SDK and broker together because
capability contract 14 intentionally rejects the 0.8.1 wire surface.

## 0.8.1 breaking migration

No aliases or deprecation shims are provided.

| Removed or renamed | 0.8.1 replacement |
| --- | --- |
| `search(..., domains=...)` | `include_domains=...` |
| `search.many(..., limit_per_query=...)` | `limit=...` |
| fusion `batch_index` | `input_index` |
| `content.get_many(sources)` | `content.fetch_many(sources)` |
| `content.read(..., offset, limit)` | `start_line`, `start_character`, `line_count` |
| `content.read_many(...)` | loop over `content.read(...)` |
| `content.grep(sources, pattern, context, max_matches_per_source)` | `grep(pattern, sources=..., context_lines=..., limit_per_source=...)` |
| `content.passages(query, sources, max_per_source)` | keyword `sources=...`, `limit_per_source=...` |
| `llm.complete_many(...)` | loop over `llm.complete(...)` |
| legacy broker `llm.extract_many(...)` | SDK `llm.extract_many(...)` over unary `llm.extract` |
| `state.merge_jsonl(...)` | `workspace.upsert_jsonl(...)` |
