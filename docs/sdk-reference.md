# OpenSAC SDK reference

This reference covers the current bundled `opensac_sdk` interface on `main`. Capability contract 15
requires a matching SDK and broker; sandbox contract 14 remains unchanged.

## Conventions

Import the synchronous SDK with:

```python
from opensac_sdk import BrokerError, sdk
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
enforced by the broker and reported by `sdk.capabilities()`.

- Local argument errors raise `ValueError`.
- Provider, quota, transport, extraction JSON/schema, and repair failures raise `BrokerError` with
  `code`, `retryable`, `attempts`, `provider`, `component`, and `scope` details when available.
- `search.many`, `content.fetch_many`, and `llm.extract_many` are public aligned fan-out helpers.
  Loop in Python for independent reads or free-form completions.

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
) -> list[Record]
```

`offset` is ranking depth, not a page number. `include_domains` is accepted only when the active
backend reports support for domain filtering.

Each hit contains `source`, `backend`, `title`, `domain`, `date`, `snippet`, `score`, `rank`,
`retrieval`, and `metadata`. An empty list is a successful no-match result.

### `sdk.search.many(...)`

```python
sdk.search.many(
    queries: list[str],
    *,
    limit: int = 10,
    offset: int = 0,
    concurrency: int = 5,
    include_domains: list[str] | None = None,
) -> list[Record]
```

The returned list is aligned one-to-one with `queries`; list position is the input identity. Every
outcome has `query`, `status`, `hits`, and `error`:

```python
[
    {"query": "q1", "status": "success", "hits": [...], "error": None},
    {
        "query": "q2",
        "status": "failure",
        "hits": [],
        "error": {
            "code": "provider_timeout",
            "message": "...",
            "retryable": True,
            "attempts": 2,
            "provider_status": None,
            "retry_after_seconds": None,
            "provider": "example",
            "component": "search",
            "scope": "provider",
        },
    },
]
```

`status` is exactly `"success"` or `"failure"`. On success, `error` is `None`; on failure, it is a
bounded structured record. Read failure details from `error.code` and `error.message`, rather than
displaying or parsing `status`. Empty `hits` with success status is a valid no-match result.

Provider, quota, and deadline errors remain item outcomes. If every item fails with a transport,
protocol, contract, or permission error, `many` raises one representative top-level `BrokerError`.

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
    report,
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
row contains `input_index`, `query`, `backend`, `rank`, and `score`. `input_index` is derived from
outcome list position; failed outcomes are skipped, while `weights` still aligns with every outcome.

## Content

Pass source strings to content operations. Local document IDs must have been admitted by search in
the current session. Web deployments may additionally admit bounded public HTTP(S) URLs according
to host policy.

### `sdk.content.fetch(...)`

```python
sdk.content.fetch(source: str) -> Record
```

Returns one complete normalized document with `source`, `text`, `title`, `date`, and provider-owned
`metadata`. A fetch failure raises `BrokerError`. Repeated calls for the same source can reuse the
session cache, although each request still consumes the public content-fetch budget.

### `sdk.content.fetch_many(...)`

```python
sdk.content.fetch_many(
    sources: list[str],
    *,
    concurrency: int = 5,
) -> list[Record]
```

This SDK helper makes bounded concurrent calls to unary `content.fetch`. It preserves input order
and duplicate sources, performs no capability-manifest preflight, and returns `[]` for empty input.
`concurrency` bounds only SDK worker fan-out; broker budget, retry, cache, trace, and provider
concurrency policies remain authoritative for each request.

Each input has one aligned outcome:

```python
[
    {
        "source": "source_1",
        "status": "success",
        "document": {"source": "source_1", "text": "...", "metadata": {}},
        "error": None,
    },
    {
        "source": "source_2",
        "status": "failure",
        "document": None,
        "error": {
            "code": "provider_timeout",
            "message": "...",
            "retryable": True,
            "attempts": 2,
            "provider_status": None,
            "retry_after_seconds": None,
            "provider": "example",
            "component": "document",
            "scope": "provider",
        },
    },
]
```

Provider, quota, and deadline failures remain per-item outcomes. If every item fails with a
transport, protocol, contract, or permission error, the helper raises one representative
`BrokerError`. Unexpected non-`BrokerError` exceptions propagate.

### `sdk.content.read(...)`

```python
sdk.content.read(
    source: str,
    *,
    start_line: int = 1,
    start_character: int = 0,
    line_count: int = 200,
    max_chars: int = 100_000,
) -> Record
```

Lines are 1-based. Characters are 0-based and end-exclusive. The returned content slice contains
the document fields plus an independent `window`:

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
) -> list[Record]
```

The returned list is aligned one-to-one with `sources`. Each outcome contains `source`, `title`,
`status`, `matches`, and `next_start_line`:

```python
[
    {
        "source": "source_1",
        "title": "Example",
        "status": "success",
        "matches": [...],
        "next_start_line": 42,
    },
    {
        "source": "source_2",
        "title": None,
        "status": "failure[provider_not_found]: ...",
        "matches": [],
        "next_start_line": None,
    },
]
```

A match contains 1-based `line`, `text`, `before`, `after`, and `spans`; its source and title come
from the owning outcome. Each span contains 0-based, end-exclusive `start_character` and
`end_character`. On a successful outcome, continue a capped scan from non-null `next_start_line`;
`None` means that source was scanned to EOF. Zero matches with success status is not a failure.
For grep outcomes, compare `status` with `"success"`; other values are displayable failure
descriptions and should not be parsed.

### `sdk.content.passages(...)`

```python
sdk.content.passages(
    query: str,
    *,
    sources: list[str],
    limit: int = 20,
    limit_per_source: int = 3,
) -> Record
```

The broker deduplicates sources in first-seen order, ranks passages globally, then applies the
per-source cap. The report contains `query`, `passages`, `failures`, `warnings`, `input_count`, and
`unique_source_count`. Passage rows include source metadata, exact `text`, coordinates, `rank`,
`score`, and `ranker`. A reranker failure falls back to lexical BM25 and appears in `warnings`.

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
) -> str
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
) -> dict[str, Any]
```

`item` and `schema` must be strict-JSON serializable, and the schema root must describe an object.
The method returns the validated object directly. `repair_attempts=1` permits one broker-managed
repair. Every initial or repair model attempt reserves quota before dispatch. Invalid provider
output, non-JSON output, schema mismatch, exhausted repair, provider failure, and quota exhaustion
are surfaced as `BrokerError` without hiding the specific code and attempt count.

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
) -> list[Record]
```

Every item shares the instruction, schema, token bound, and repair policy. All items are validated
as strict JSON before fan-out, while original items are omitted from outcomes and diagnostics.
Each item remains an independent unary `llm.extract` request, so broker quota, retries, tracing,
and provider concurrency remain authoritative.

The returned list is input-aligned. Each outcome contains `input_index`, `status`, `data`, and
`error`. Status is exactly `"success"` or `"failure"`; success has schema-validated `data` and
`error=None`, while failure has `data=None` and a structured `error`. Provider, schema, quota, and
deadline failures remain per-item outcomes. If every item fails with a transport, protocol,
contract, or permission error, the helper raises one representative `BrokerError`.

## Capabilities

### `sdk.capabilities()`

Returns contract versions, search backend support, content/LLM upper limits, and active mechanism
switches. Generated programs should inspect this record instead of hard-coding deployment maxima.

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
