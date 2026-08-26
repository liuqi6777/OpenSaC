# OpenSAC SDK API Reference

This document covers all 22 public operations declared by `SDK_SURFACE` in the bundled
`opensac_sdk` 0.8.0. The SDK is a synchronous interface for generated programs running inside an
OpenSAC sandbox. Host and sandbox images provide a version-matched SDK.

## 1. Entry point, return values, and errors

```python
from opensac_sdk import BrokerError, __version__, sdk
```

The package root exports only `sdk`, `BrokerError`, and `__version__`. `sdk` is a lazily initialized
singleton with six namespaces: `search`, `content`, `llm`, `session`, `state`, and `output`.

JSON objects returned by the broker are recursively wrapped as `Record` values. `Record` is a
`dict` subclass, so fields support both attribute and mapping access:

```python
hits = sdk.search("OpenSAC", limit=3)
source = hits[0].source
assert source == hits[0]["source"]
plain_dict = dict(hits[0])
```

Arrays remain ordinary `list` values. Scalars remain ordinary `str`, `int`, `float`, `bool`, or
`None` values.

Broker-level failures raise `BrokerError`:

```python
try:
    hits = sdk.search("query")
except BrokerError as error:
    print(
        error.code,
        error.retryable,
        error.attempts,
        error.provider_status,
        error.retry_after_seconds,
        error.provider,
        error.component,
        error.scope,
    )
```

Local argument errors normally raise `ValueError`. Reading a missing state file raises
`FileNotFoundError`. Failure-aware batch operations return separate `results` and `failures` lists;
their `input_index` values partition the original inputs. `sac_run` automatically renders bounded
failure warnings before stdout. A failed single-item call raises `BrokerError`, while a legitimate
empty result has no typed failure and no warning.

Arguments are validated without coercion: for example, `"10"` is not accepted where an integer is
required and `True` is not accepted as an integer. Values written to state or output must be strict
JSON; unsupported objects, NaN, and Infinity raise `ValueError` before an existing artifact changes.

### Public operation overview

| Tier | Operations |
| --- | --- |
| core | `sdk.search`, `sdk.search.many`, `sdk.content.passages`, `sdk.content.read`, `sdk.content.read_many`, `sdk.content.grep`, `sdk.llm.extract_many`, `sdk.session.usage`, `sdk.session.capabilities`, `sdk.output.submit` |
| helper | `sdk.search.fuse_rrf` and every public `sdk.state.*` operation |
| advanced | `sdk.content.get_many`, `sdk.llm.complete`, `sdk.llm.complete_many` |

Use core operations for normal generated programs. Helpers are deterministic local operations.
Advanced operations are intended for cases that genuinely require full document bodies or
free-form model responses.

Every method entry below follows the same order: tier, signature, parameters, returns, behavior and
errors, then example.

## 2. Common public data shapes

The following JSON shapes are reused throughout this document. `None` marks a nullable value.

### `SearchHit`

```python
{
    "source": str,       # The one public address accepted by content operations
    "backend": str,
    "title": str,
    "domain": str | None,
    "date": str | None,  # Provider value; not normalized to one date format
    "snippet": str,
    "score": float | None,
    "rank": int,         # 1-based
    "retrieval": {
        "mode": str | None,
        "result_mode": str | None,
        "score_name": str | None,
        "higher_is_better": bool | None,
        "comparable_across_queries": bool | None,
    } | None,
    "metadata": dict,
}
```

### `CapabilityFailure`

```python
{
    "code": str,
    "message": str,
    "retryable": bool,
    "attempts": int,
    "provider_status": int | None,       # May be absent
    "retry_after_seconds": float | None, # May be absent
    "provider": str | None,              # Secret-free upstream name
    "component": str | None,             # Diagnostic label, for example document
    "scope": "request" | "resource" | "provider" | "unknown" | None,
}
```

`scope` is the safest actionable layer supported by transport evidence: `request` means caller
input, `resource` means one query/document, and `provider` means shared service, credentials, or
capacity. `unknown` is intentional when an HTTP status cannot distinguish those causes, such as a
Jina Reader 403. Provider response bodies and credential-bearing details are never exposed.

### `ContentRow`

```python
{
    "source": str,
    "text": str,
    "title": str,
    "date": str | None,
    "metadata": dict,
}
```

This shape represents only a successful fetch. A failed single-source read raises `BrokerError`;
batch failures use `ContentFailure`.

### `ContentFailure`

```python
{
    "input_index": int,
    "source": str,
    # ...CapabilityFailure fields
}
```

### `ContentReadWindow`

```python
{
    "source": str,
    "offset": int,      # Optional, defaults to 1
    "limit": int,       # Optional, defaults to 200
    "max_chars": int,   # Optional, defaults to 100_000
}
```

## 3. `sdk.search`

### `sdk.search(...)`

**Tier:** `core`

**Signature**

```python
sdk.search(
    query: str,
    *,
    limit: int = 10,
    offset: int = 0,
    domains: list[str] | None = None,
) -> list[Record]
```

**Parameters**

| Name | Default | Description |
| --- | --- | --- |
| `query` | required | Non-empty search query. |
| `limit` | `10` | Number of hits in this ranking window. |
| `offset` | `0` | Depth in the complete ranking, not a page number. |
| `domains` | `None` | Optional backend-side domain allowlist. |

**Returns**

`list[Record]`: `SearchHit` records ordered by `rank`.

**Behavior and errors**

- An empty list is a successful no-match result.
- `domains` is accepted only when the current backend supports domain filtering; an unsupported
  filter fails instead of being ignored.
- `limit` has an effective maximum of 100 and `offset` has an effective maximum of 500. The backend
  may impose a smaller maximum retrieval depth.
- Request-wide failures raise `BrokerError`.

**Example**

```python
hits = sdk.search("Who introduced ReAct prompting?", limit=10)
sources = [hit.source for hit in hits]
```

### `sdk.search.many(...)`

**Tier:** `core`

**Signature**

```python
sdk.search.many(
    queries: list[str],
    *,
    limit_per_query: int = 10,
    offset: int = 0,
    concurrency: int = 5,
    domains: list[str] | None = None,
) -> Record
```

**Parameters**

| Name | Default | Description |
| --- | --- | --- |
| `queries` | required | Queries to execute; report outcomes preserve their input positions. |
| `limit_per_query` | `10` | Number of hits in each query's ranking window. |
| `offset` | `0` | Ranking depth applied to every query. |
| `concurrency` | `5` | Maximum requested broker-side concurrency. |
| `domains` | `None` | Optional backend-side domain allowlist for every query. |

**Returns**

`Record`: an input-partitioned report:

```python
{
    "results": [
        {"input_index": int, "query": str, "hits": list[SearchHit]}
    ],
    "failures": [
        {"input_index": int, "query": str, ...CapabilityFailure fields}
    ],
    "input_count": int,
}
```

**Behavior and errors**

- An empty `hits` list in `results` means a successful query without matches. Failed queries appear
  only in `failures`.
- `concurrency` is effectively bounded from 1 through 20.
- A default deployment accepts at most 64 queries per request; the host can configure this limit.
- External query failures, including 0/N successful queries, remain input-indexed and produce an
  automatic execution warning. A failure that prevents a safe report raises `BrokerError`.

**Example**

```python
report = sdk.search.many(
    ["ReAct paper", "ReAct prompting authors"],
    limit_per_query=10,
    concurrency=2,
)

for failure in report.failures:
    print(failure.query, failure.code)
```

### `sdk.search.fuse_rrf(...)`

**Tier:** `helper`

**Signature**

```python
sdk.search.fuse_rrf(
    report: Record | dict,
    *,
    weights: list[float] | None = None,
    k: int = 60,
    limit: int | None = None,
    exclude_domains: list[str] | None = None,
    domain_weights: dict[str, float] | None = None,
    max_per_domain: int | None = None,
) -> list[Record]
```

**Parameters**

| Name | Default | Description |
| --- | --- | --- |
| `report` | required | The report returned by `search.many`. |
| `weights` | `None` | Non-negative weights aligned with the report's original inputs. |
| `k` | `60` | Non-negative RRF rank-smoothing constant. |
| `limit` | `None` | Optional final candidate count. |
| `exclude_domains` | `None` | Hostnames and subdomains to remove. |
| `domain_weights` | `None` | Positive score multipliers keyed by hostname. |
| `max_per_domain` | `None` | Optional cap per exact Web hostname. |

**Returns**

`list[Record]`: fused candidates that preserve the representative `SearchHit` fields and add:

```python
{
    # ...SearchHit fields
    "provenance": [
        {
            "batch_index": int,
            "query": str,
            "backend": str,
            "rank": int,
            "score": float | None,
        }
    ],
    "raw_fused_score": float,
    "domain_weight": float,
    "fused_score": float,
    "fused_rank": int,
}
```

**Behavior and errors**

- This helper is deterministic and local: it makes no broker call and incurs no provider work.
- Only `report.results` participate in fusion. `search.many` has already recorded any report
  failures, so this local helper neither interprets nor emits them again.
- Domain policies match an exact hostname and its subdomains. Non-Web sources are unaffected.
- Domain policy is applied before the final `limit`.
- Invalid weights, ranks, limits, or domain policies raise `ValueError`.

**Example**

```python
fused = sdk.search.fuse_rrf(
    report,
    weights=[1.0, 1.5],
    exclude_domains=["social.example"],
    domain_weights={"docs.example.com": 2.0},
    max_per_domain=3,
    limit=20,
)
```

## 4. `sdk.content`

Content methods accept source strings, never `SearchHit` records. `read` accepts exactly one source;
batch methods accept either `list[str]` or, for `read_many`, a list of `ContentReadWindow` objects.
Each source is limited to 4096 characters. A default deployment accepts at most 256 batch items,
but the host can configure this limit.

A Web deployment may accept public HTTP(S) URLs directly, depending on host policy. A local document
ID must first be admitted by search in the current session. `get_many`, `read_many`, and `grep`
preserve duplicate input positions while allowing the broker to reuse fetch work. `passages`
deduplicates by first appearance.

### `sdk.content.get_many(...)`

**Tier:** `advanced`

**Signature**

```python
sdk.content.get_many(sources: list[str]) -> Record
```

**Parameters**

| Name | Default | Description |
| --- | --- | --- |
| `sources` | required | Source strings to fetch in input order. |

**Returns**

`Record`: a report with successful full-document rows in `results`, flat `ContentFailure` rows in
`failures`, and `input_count`. Both outcome lists carry `input_index`.

**Behavior and errors**

- Duplicate inputs keep separate result positions while fetch work may be reused.
- Prefer `passages` for evidence discovery and `read` for bounded line windows.
- External fetch failures, including 0/N successful sources, remain input-indexed and produce an
  automatic execution warning.

**Example**

```python
report = sdk.content.get_many([hit.source for hit in hits])
for row in report.results:
    process(row.text)
```

### `sdk.content.read(...)`

**Tier:** `core`

**Signature**

```python
sdk.content.read(
    source: str,
    *,
    offset: int = 1,
    limit: int = 200,
    max_chars: int = 100_000,
) -> Record
```

**Parameters**

| Name | Default | Description |
| --- | --- | --- |
| `source` | required | The one source string to read. |
| `offset` | `1` | First line in the window; line numbers are 1-based. |
| `limit` | `200` | Maximum number of lines per source. |
| `max_chars` | `100_000` | Maximum response characters per source. |

**Returns**

`Record`: one `ContentRow`. A successful row adds at least these `metadata` fields:

```python
{
    "start_line": int,       # 0 for an empty window
    "end_line": int,
    "total_lines": int,
    "next_offset": int | None,
    "truncated_by_max_chars": bool,        # Present only when truncated
    "truncated_mid_line": bool,            # Present only for a partial long line
    "partial_line_remaining_chars": int,   # Present only for a partial long line
}
```

**Behavior and errors**

- Values are effectively constrained to `offset >= 1`, `1 <= limit <= 5000`, and
  `1 <= max_chars <= 400000`.
- `next_offset is None` means the end of the document has been reached.
- An external fetch failure raises `BrokerError`; no synthetic empty success row is returned.

**Example**

```python
offset = 1
while offset is not None:
    row = sdk.content.read(source, offset=offset, limit=200)
    consume(row.text)
    offset = row.metadata.next_offset
```

### `sdk.content.read_many(...)`

**Tier:** `core`

**Signature**

```python
sdk.content.read_many(windows: list[ContentReadWindow]) -> Record
```

**Parameters**

| Name | Default | Description |
| --- | --- | --- |
| `windows` | required | Source-specific read windows in input order. Unknown fields are rejected. |

**Returns**

`Record`: a report with successful `ContentRow` values in `results`, flat `ContentFailure` values
in `failures`, and `input_count`. Both outcome lists carry `input_index`; each window applies its
own `offset`, `limit`, and `max_chars`.

**Behavior and errors**

- Duplicate sources keep separate result positions and independent slices while broker fetch work
  may be reused.
- Missing window options use the same defaults and bounds as `read`.
- Invalid windows raise `ValueError`; external fetch failures remain input-indexed and produce an
  automatic execution warning.

**Example**

```python
report = sdk.content.read_many(
    [
        {"source": first_source, "offset": 1, "limit": 80},
        {"source": second_source, "offset": 120, "limit": 40, "max_chars": 16_000},
    ]
)
```

### `sdk.content.grep(...)`

**Tier:** `core`

**Signature**

```python
sdk.content.grep(
    sources: list[str],
    pattern: str,
    *,
    mode: Literal["regex", "literal"] = "regex",
    case_sensitive: bool = False,
    context: int = 0,
    max_matches_per_source: int = 20,
) -> Record
```

**Parameters**

| Name | Default | Description |
| --- | --- | --- |
| `sources` | required | Source strings to inspect in input order. |
| `pattern` | required | Non-empty pattern. |
| `mode` | `"regex"` | Explicit matching mode: `"regex"` or `"literal"`. |
| `case_sensitive` | `False` | Whether matching preserves case. |
| `context` | `0` | Number of surrounding lines on each side. |
| `max_matches_per_source` | `20` | Maximum matches contributed by each input source. |

**Returns**

`Record`: a grep report with this shape:

```python
{
    "pattern": str,
    "mode": "regex" | "literal",
    "case_sensitive": bool,
    "context": int,
    "max_matches_per_source": int,
    "matches": [
        {
            "source": str,
            "title": str,
            "line": int,          # 1-based; directly usable as read(offset=...)
            "text": str,
            "before": list[str],
            "after": list[str],
            "input_index": int,
        }
    ],
    "source_results": [
        {
            "input_index": int,
            "source": str,
            "title": str,
            "match_count": int,
            "scan_complete": bool,
        }
    ],
    "failures": [ContentFailure],
    "input_count": int,
}
```

**Behavior and errors**

- Matching is line-based. Invalid regular expressions raise `ValueError` in regex mode; literal
  mode never interprets regular-expression metacharacters.
- `context` is effectively bounded from 0 through 20; `max_matches_per_source` is bounded from 1
  through 200.
- `source_results` contains successful scans. `scan_complete=True` with `match_count=0` is a
  successful zero-match scan; a capped successful scan has `scan_complete=False`. Fetch failures
  appear separately in `failures`.
- Duplicate sources remain distinguishable through `input_index`.
- External fetch failures, including 0/N successful scans, remain input-indexed in `failures` and produce
  an automatic execution warning.

**Example**

```python
report = sdk.content.grep(sources, r"born in \d{4}", context=2)
for match in report.matches:
    window = sdk.content.read(
        match.source,
        offset=max(1, match.line - 5),
        limit=11,
    )
```

### `sdk.content.passages(...)`

**Tier:** `core`

**Signature**

```python
sdk.content.passages(
    query: str,
    sources: list[str],
    *,
    limit: int = 20,
    max_per_source: int = 3,
) -> Record
```

**Parameters**

| Name | Default | Description |
| --- | --- | --- |
| `query` | required | Non-empty passage-ranking query. |
| `sources` | required | Caller-authorized source strings. |
| `limit` | `20` | Maximum passage count across the report. |
| `max_per_source` | `3` | Maximum passages contributed by one source. |

**Returns**

`Record`: a passage report with this shape:

```python
{
    "query": str,
    "passages": [
        {
            "source": str,
            "title": str,
            "date": str | None,
            "text": str,
            "coordinates": {
                "start_line": int,       # 1-based
                "start_character": int,  # 0-based
                "end_line": int,         # 1-based
                "end_character": int,    # 0-based and exclusive
            },
            "rank": int,
            "score": float,
            "ranker": str,
        }
    ],
    "failures": list[ContentFailure],
    "warnings": list[CapabilityFailure], # Reranker fallback diagnostics
    "input_count": int,
    "unique_source_count": int,
}
```

**Behavior and errors**

- Sources are deduplicated at their first input position before ranking.
- `limit` must be between 1 and 100; `max_per_source` must be between 1 and 10.
- Scores are comparable only within one report.
- `coordinates` is a half-open range in normalized document text.
- Fetch failures appear in `failures` and produce an automatic execution warning, including when
  no source succeeds.
- A configured reranker failure falls back to `lexical:bm25`; its typed diagnostic appears in
  `warnings` and is also rendered before stdout.

**Example**

```python
report = sdk.content.passages(
    "original authors and publication date",
    sources,
    limit=20,
    max_per_source=3,
)
```

## 5. `sdk.llm`

These operations use the optional pipeline model configured by the host. An unconfigured model, a
whole-call provider failure, or an exhausted budget raises `BrokerError`. Prefer deterministic
Python whenever it is sufficient, and prefer `extract_many` when downstream code needs structured
data.

### `sdk.llm.complete(...)`

**Tier:** `advanced`

**Signature**

```python
sdk.llm.complete(
    prompt: str,
    *,
    system: str | None = None,
    temperature: float = 0.2,
    max_tokens: int | None = None,
) -> str
```

**Parameters**

| Name | Default | Description |
| --- | --- | --- |
| `prompt` | required | Non-empty user prompt. |
| `system` | `None` | Optional system instruction. |
| `temperature` | `0.2` | Sampling temperature. |
| `max_tokens` | `None` | Optional completion-token ceiling. |

**Returns**

`str`: the model's response text.

**Behavior and errors**

- `temperature` is effectively bounded from 0.0 through 2.0.
- `max_tokens` is effectively bounded from 1 through 32000 and may be reduced by session budget.
- An unconfigured model, provider failure, or exhausted budget raises `BrokerError`.

**Example**

```python
summary = sdk.llm.complete(
    "Summarize this evidence:\n" + evidence,
    system="Be concise and preserve numbers.",
    max_tokens=300,
)
```

### `sdk.llm.complete_many(...)`

**Tier:** `advanced`

**Signature**

```python
sdk.llm.complete_many(
    prompts: list[str],
    *,
    system: str | None = None,
    temperature: float = 0.2,
    max_tokens: int | None = None,
    concurrency: int = 4,
) -> list[str]
```

**Parameters**

| Name | Default | Description |
| --- | --- | --- |
| `prompts` | required | Non-empty prompts; results preserve this order. |
| `system` | `None` | Optional system instruction shared by every prompt. |
| `temperature` | `0.2` | Sampling temperature shared by every prompt. |
| `max_tokens` | `None` | Optional per-response completion-token ceiling. |
| `concurrency` | `4` | Maximum requested model concurrency. |

**Returns**

`list[str]`: one response per prompt in input order.

**Behavior and errors**

- An empty input list returns `[]`; individual prompts must not be empty.
- `concurrency` is effectively bounded from 1 through 12.
- An unconfigured model, batch provider failure, or exhausted budget raises `BrokerError`.

**Example**

```python
summaries = sdk.llm.complete_many(prompts, concurrency=4, max_tokens=200)
```

### `sdk.llm.extract_many(...)`

**Tier:** `core`

**Signature**

```python
sdk.llm.extract_many(
    items: list[Any],
    *,
    instruction: str,
    schema: dict[str, Any],
    concurrency: int = 4,
    max_tokens: int | None = None,
    repair_attempts: int = 0,
) -> Record
```

**Parameters**

| Name | Default | Description |
| --- | --- | --- |
| `items` | required | Strictly JSON-serializable inputs. |
| `instruction` | required | Extraction instruction shared by every item. |
| `schema` | required | Supported object-root JSON Schema. |
| `concurrency` | `4` | Maximum requested model concurrency. |
| `max_tokens` | `None` | Optional per-attempt completion-token ceiling. |
| `repair_attempts` | `0` | Additional repair pass: either `0` or `1`. |

**Returns**

`Record`: an input-partitioned extraction report:

```python
{
    "results": [
        {"input_index": int, "data": dict, "attempts": int}
    ],
    "failures": [
        {"input_index": int, "attempts": int, ...CapabilityFailure fields}
    ],
    "input_count": int,
}
```

**Behavior and errors**

- Successful and failed rows are separate; together their `input_index` values partition the input.
- `repair_attempts=1` adds one repair pass for repairable formatting or schema errors.
- `items` and `schema` cannot contain NaN or Infinity. The schema root must declare
  `{"type": "object"}`.
- Supported schema keywords are `$schema`, `type`, `properties`, `required`,
  `additionalProperties`, `items`, `enum`, and `description`.
- A default deployment accepts at most 256 items; the host can configure size and nesting limits.
- Per-item provider failures may coexist with success. If every item fails, the report still
  returns every failure and `sac_run` renders a 0/N execution warning.

**Example**

```python
report = sdk.llm.extract_many(
    passages,
    instruction="Extract whether the passage names an author.",
    schema={
        "type": "object",
        "properties": {
            "has_author": {"type": "boolean"},
            "author": {"type": ["string", "null"]},
        },
        "required": ["has_author", "author"],
        "additionalProperties": False,
    },
    repair_attempts=1,
)
```

## 6. `sdk.session`

### `sdk.session.usage()`

**Tier:** `core`

**Signature**

```python
sdk.session.usage() -> dict[str, Any]
```

**Parameters**

None.

**Returns**

`Record`: current session strategy usage, remaining allowances, and terminal state:

```python
{
    "exec_calls": int,
    "search_calls": int,
    "content_fetches": int,
    "content_backend_fetches": int,
    "direct_url_attempts": int,
    "direct_url_successes": int,
    "llm_calls": int,
    "pipeline_model_tokens": int,
    "pipeline_output_tokens_reserved": int,
    "sandbox_seconds": float,
    "workspace_bytes": int,
    "documents_seen": int,
    "budget_consumed": {
        "max_exec_calls": int,
        "max_search_queries": int,
        "max_content_fetches": int,
        "max_pipeline_llm_calls": int,
        "max_pipeline_output_tokens": int,
        "max_sandbox_seconds": float,
        "max_workspace_bytes": int,
    },
    "budget_remaining": {
        "max_exec_calls": int | None,
        "max_search_queries": int | None,
        "max_content_fetches": int | None,
        "max_pipeline_llm_calls": int | None,
        "max_pipeline_output_tokens": int | None,
        "max_sandbox_seconds": float | None,
        "max_workspace_bytes": int | None,
    },
    "provider": {
        "attempts_by_capability": dict[str, int],
        "retries": int,
        "intra_call_deduplicated_items": int,
        "coalesced_requests": int,
        "queue_seconds": float,
        "rate_limit_wait_seconds": float,
        "backoff_seconds": float,
    },
    "terminal_reason": str | None,
}
```

**Behavior and errors**

- `None` in `budget_remaining` means the resource has no hard ceiling.
- `provider.attempts_by_capability` attributes backend attempts to the calling capability family,
  such as `search`, `content`, or `llm`; absent families made no backend attempt.
- A broker read failure raises `BrokerError`.

**Example**

```python
usage = sdk.session.usage()
if usage.budget_remaining.max_search_queries == 0:
    stop_searching()
```

### `sdk.session.capabilities()`

**Tier:** `core`

**Signature**

```python
sdk.session.capabilities() -> dict[str, Any]
```

**Parameters**

None.

**Returns**

`Record`: the session's public contract versions, active search backend and limits, content policy
and limits, structured-extraction availability and limits, and enabled mechanisms:

```python
{
    "contracts": {"sandbox": int, "capability": int},
    "search": {"backend": str, "supports_domains": bool, "max_depth": int | None, "limits": dict},
    "content": {"url_admission": str, "limits": dict},
    "llm": {"available": bool, "limits": dict},
    "mechanisms": dict,
}
```

**Behavior and errors**

- The manifest is built from the same active configuration used by the host; it contains no
  credentials or provider secrets.
- A broker read failure raises `BrokerError`.

**Example**

```python
capabilities = sdk.session.capabilities()
batch_limit = capabilities.content.limits.max_sources_per_request
```

## 7. `sdk.state`

State methods are local file operations and make no broker call. Paths are relative to the current
session workspace and cannot escape it through `..` or similar traversal. State is program memory
for one live session, not a cross-session database. Local document sources also become invalid if
the host reports `state_lost`.

### `sdk.state.write_jsonl(...)`

**Tier:** `helper`

**Signature**

```python
sdk.state.write_jsonl(relative_path: str, rows: list[Any]) -> None
```

**Parameters**

| Name | Default | Description |
| --- | --- | --- |
| `relative_path` | required | Workspace-relative JSONL path. |
| `rows` | required | Values to serialize as JSONL rows. |

**Returns**

`None`.

**Behavior and errors**

- Strictly encodes every row before atomically replacing the file and creates missing parent
  directories. A serialization failure leaves an existing file unchanged.
- A path that escapes the workspace raises `ValueError`.

**Example**

```python
sdk.state.write_jsonl("queries.jsonl", [{"query": "alpha"}])
```

### `sdk.state.append_jsonl(...)`

**Tier:** `helper`

**Signature**

```python
sdk.state.append_jsonl(relative_path: str, rows: list[Any]) -> None
```

**Parameters**

| Name | Default | Description |
| --- | --- | --- |
| `relative_path` | required | Workspace-relative JSONL path. |
| `rows` | required | Values to append as JSONL rows. |

**Returns**

`None`.

**Behavior and errors**

- Strictly encodes the complete input before appending without deduplication. A serialization
  failure appends nothing; the file and parent directories are created when absent.
- A path that escapes the workspace raises `ValueError`.

**Example**

```python
sdk.state.append_jsonl("queries.jsonl", [{"query": "beta"}])
```

### `sdk.state.merge_jsonl(...)`

**Tier:** `helper`

**Signature**

```python
sdk.state.merge_jsonl(
    relative_path: str,
    rows: list[Any],
    key: str = "source",
) -> int
```

**Parameters**

| Name | Default | Description |
| --- | --- | --- |
| `relative_path` | required | Workspace-relative JSONL path. |
| `rows` | required | Object rows to upsert. |
| `key` | `"source"` | Field that identifies the same logical row. |

**Returns**

`int`: total row count after the merge.

**Behavior and errors**

- Repeated keys replace their rows without changing first-seen key order; the replacement is
  atomic.
- Every new row must be an object containing `key`.
- A missing key or path that escapes the workspace raises `ValueError`.

**Example**

```python
count = sdk.state.merge_jsonl("pool.jsonl", hits, key="source")
```

### `sdk.state.exists(...)`

**Tier:** `helper`

**Signature**

```python
sdk.state.exists(relative_path: str) -> bool
```

**Parameters**

| Name | Default | Description |
| --- | --- | --- |
| `relative_path` | required | Workspace-relative path to check. |

**Returns**

`bool`: `True` only when the path is an existing file.

**Behavior and errors**

- Directories and absent paths return `False`.
- A path that escapes the workspace raises `ValueError`.

**Example**

```python
if sdk.state.exists("pool.jsonl"):
    pool = sdk.state.read_jsonl("pool.jsonl")
```

### `sdk.state.list(...)`

**Tier:** `helper`

**Signature**

```python
sdk.state.list(prefix: str = "") -> list[str]
```

**Parameters**

| Name | Default | Description |
| --- | --- | --- |
| `prefix` | `""` | String prefix applied to workspace-relative paths. |

**Returns**

`list[str]`: sorted workspace-relative file paths.

**Behavior and errors**

- The search is recursive; an absent workspace returns `[]`.
- Runtime files whose names start with `.opensac-` are hidden.

**Example**

```python
artifacts = sdk.state.list("pool")
```

### `sdk.state.read_jsonl(...)`

**Tier:** `helper`

**Signature**

```python
sdk.state.read_jsonl(relative_path: str) -> list[Any]
```

**Parameters**

| Name | Default | Description |
| --- | --- | --- |
| `relative_path` | required | Workspace-relative JSONL path. |

**Returns**

`list[Any]`: non-empty JSONL rows, with objects recursively wrapped as `Record` values.

**Behavior and errors**

- Empty lines are ignored.
- A missing file raises `FileNotFoundError`; an invalid line or escaping path raises `ValueError`.

**Example**

```python
pool = sdk.state.read_jsonl("pool.jsonl")
```

### `sdk.state.write_json(...)`

**Tier:** `helper`

**Signature**

```python
sdk.state.write_json(relative_path: str, value: Any) -> None
```

**Parameters**

| Name | Default | Description |
| --- | --- | --- |
| `relative_path` | required | Workspace-relative JSON path. |
| `value` | required | Value to serialize. |

**Returns**

`None`.

**Behavior and errors**

- Strictly encodes the value before atomically replacing the file and creates missing parent
  directories. A serialization failure leaves an existing file unchanged.
- A path that escapes the workspace raises `ValueError`.

**Example**

```python
sdk.state.write_json("progress.json", {"offset": 20})
```

### `sdk.state.read_json(...)`

**Tier:** `helper`

**Signature**

```python
sdk.state.read_json(relative_path: str) -> Any
```

**Parameters**

| Name | Default | Description |
| --- | --- | --- |
| `relative_path` | required | Workspace-relative JSON path. |

**Returns**

`Any`: parsed JSON with objects recursively wrapped as `Record` values.

**Behavior and errors**

- A missing file raises `FileNotFoundError`; invalid JSON or an escaping path raises `ValueError`.

**Example**

```python
progress = sdk.state.read_json("progress.json")
```

## 8. `sdk.output`

### `sdk.output.submit(...)`

**Tier:** `core`

**Signature**

```python
sdk.output.submit(
    output: Any,
    *,
    citations: list[str] | None = None,
) -> None
```

**Parameters**

| Name | Default | Description |
| --- | --- | --- |
| `output` | required | Final value to serialize. |
| `citations` | `None` | Optional URL/source strings declared by the caller. |

**Returns**

`None`. The method atomically writes this final output artifact:

```python
{
    "output": Any,
    "citations": list[str],
}
```

**Behavior and errors**

- `citations` accepts at most 256 strings with at most 4096 characters each.
- Citations are unverified labels; this method does not fetch, resolve, or validate source support.
- A later call atomically replaces the earlier submission.
- Malformed citations or non-JSON output (including NaN and Infinity) raise `ValueError` without
  changing an existing submission.

**Example**

```python
sdk.output.submit(
    {"answer": answer, "confidence": 0.9},
    citations=[passage.source for passage in report.passages],
)
```

## 9. Lifecycle and non-public entry points

`sdk.close()` closes the broker transport held by the lazy client. A later namespace access creates
a new client from the environment. Ordinary sandbox programs normally do not need to call it because
the SDK closes automatically when the process exits.

`StateResource.from_environment()`, `OutputResource.from_environment()`, transport constructors, and
the resource classes are implementation details and are not part of the public `SDK_SURFACE`. Do not
import types from internal modules. The stable public entry point is:

```python
from opensac_sdk import BrokerError, __version__, sdk
```
