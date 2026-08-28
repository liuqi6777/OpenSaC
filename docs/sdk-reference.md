# OpenSAC SDK reference

This reference covers the bundled `opensac_sdk` 0.8.1 interface. Version 0.8.1 is an intentional
breaking pre-1.0 release: capability contract 13 and sandbox contract 14 prevent older SDK,
broker, and sandbox combinations from running together.

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
enforced by the broker and reported by `sdk.session.capabilities()`.

- Local argument errors raise `ValueError`.
- Provider, quota, transport, extraction JSON/schema, and repair failures raise `BrokerError` with
  `code`, `retryable`, `attempts`, `provider`, `component`, and `scope` details when available.
- Only `search.many` is a public batch helper. Loop in Python for independent content or LLM calls.

## Public surface

| Namespace | Operations |
| --- | --- |
| Search | `search`, `search.many`, `search.fuse_rrf` |
| Content | `content.fetch`, `content.read`, `content.grep`, `content.passages` |
| LLM | `llm.complete`, `llm.extract` |
| Session | `session.usage`, `session.capabilities` |
| State | JSON/JSONL operations including `state.upsert_jsonl` |
| Output | `output.submit` |

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
outcome has `query`, `status`, and `hits`:

```python
[
    {"query": "q1", "status": "success", "hits": [...]},
    {"query": "q2", "status": "failure[provider_timeout]: ...", "hits": []},
]
```

`status` is exactly `"success"` for success. Any other string is a bounded, human-readable failure
description; callers should display or log it, not parse it. Empty `hits` with success status is a
valid no-match result. Structured failure details remain in host-side diagnostics.

`Mechanisms.batching` controls this operation only. When batching is disabled, one query is still
accepted but a wider fan-out is rejected.

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
As with search outcomes, only compare `status` with `"success"`; do not parse failure descriptions.

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

To process several items, loop explicitly:

```python
results = []
failures = []
for input_index, item in enumerate(items):
    try:
        data = sdk.llm.extract(item, instruction=instruction, schema=schema)
    except BrokerError as error:
        failures.append({"input_index": input_index, "code": error.code})
    else:
        results.append({"input_index": input_index, "data": data})
```

## Session

### `sdk.session.usage()`

Returns exactly:

```python
{
    "exec_calls": int,
    "search_calls": int,
    "content_fetches": int,
    "llm_calls": int,
    "pipeline_output_tokens_reserved": int,
    "sandbox_seconds": float,
    "workspace_bytes": int,
    "budget_remaining": {
        "max_exec_calls": int | None,
        "max_search_queries": int | None,
        "max_content_fetches": int | None,
        "max_pipeline_llm_calls": int | None,
        "max_pipeline_output_tokens": int | None,
        "max_sandbox_seconds": float | None,
        "max_workspace_bytes": int | None,
    },
    "terminal_reason": str | None,
}
```

`None` means unlimited. Provider attempts, cache behavior, queueing, retry detail, and actual model
tokens remain host-side metrics rather than public strategy state.

### `sdk.session.capabilities()`

Returns contract versions, search backend support, content/LLM upper limits, and active mechanism
switches. Generated programs should inspect this record instead of hard-coding deployment maxima.

## State and output

State paths are workspace-relative and cannot escape the session workspace.

```python
sdk.state.write_json(path, value)
sdk.state.read_json(path)
sdk.state.write_jsonl(path, rows)
sdk.state.append_jsonl(path, rows)
sdk.state.upsert_jsonl(path, rows, key="source") -> int
sdk.state.read_jsonl(path)
sdk.state.exists(path) -> bool
sdk.state.list(prefix="") -> list[str]
```

`upsert_jsonl` preserves first-seen order and replaces the complete row for an existing key; it is
not a field-level merge.

```python
sdk.output.submit(value, *, citations: list[str] | None = None) -> None
```

Submission atomically writes the current execution output. Citation strings are labels, not broker
evidence validation. Repeated submissions replace the previous output.

## 0.8.1 breaking migration

No aliases or deprecation shims are provided.

| Removed or renamed | 0.8.1 replacement |
| --- | --- |
| `search(..., domains=...)` | `include_domains=...` |
| `search.many(..., limit_per_query=...)` | `limit=...` |
| fusion `batch_index` | `input_index` |
| `content.get_many(sources)` | loop over `content.fetch(source)` |
| `content.read(..., offset, limit)` | `start_line`, `start_character`, `line_count` |
| `content.read_many(...)` | loop over `content.read(...)` |
| `content.grep(sources, pattern, context, max_matches_per_source)` | `grep(pattern, sources=..., context_lines=..., limit_per_source=...)` |
| `content.passages(query, sources, max_per_source)` | keyword `sources=...`, `limit_per_source=...` |
| `llm.complete_many(...)` | loop over `llm.complete(...)` |
| `llm.extract_many(...)` | loop over direct-returning `llm.extract(...)` |
| `state.merge_jsonl(...)` | `state.upsert_jsonl(...)` |
| `output.submit(output, ...)` | `output.submit(value, ...)` |
