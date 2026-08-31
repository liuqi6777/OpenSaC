# OpenSAC v0.8.4

OpenSAC v0.8.4 simplifies generated-program failure handling. Broker-backed SDK methods now return
their result directly or `None`; aligned fan-out helpers return one result or `None` per input
position. Operational failures are recorded as bounded structured warnings and rendered consistently
by the MCP, CLI, and minimal-agent adapters.

This generated-program API change is intentionally breaking. There is no `Outcome` compatibility
alias or deprecation shim. Capability contract 15 and sandbox contract 14 are unchanged because the
broker RPC and host/sandbox execution boundaries did not change.

## Direct optional results

Unary broker-backed methods now return `T | None`:

```python
hits = sdk.search("query")
if hits is None:
    print("NEXT: revise the query")
else:
    print(f"READY: usable={len(hits)}")
```

Aligned fan-out helpers return `list[T | None]` while preserving input order and duplicates:

```python
queries = ["first", "second"]
results = sdk.search.many(queries)
for query, hits in zip(queries, results, strict=True):
    if hits is None:
        continue
    print(query, len(hits))
```

Use `is None`, not truthiness. Successful search can return `[]`, completion can return `""`, and
extraction can return `{}`. Local argument errors and unexpected programming exceptions still
propagate; only operational `BrokerError` failures become `None`.

## Structured warnings

The SDK records sanitized, bounded failure summaries in the execution output envelope. Agent
renderers display these summaries as `[OpenSAC warning]` lines without requiring generated programs
to catch exceptions or print error records. Clean success still renders only program stdout, and
execution failures continue to render as `[OpenSAC error]` after any stdout.

The returned SDK value intentionally has no error side channel such as `sdk.last_error`. Broker and
host policies remain responsible for retries and fallbacks; generated programs branch only on
whether a result is available.

## Search fusion

`sdk.search.fuse_rrf` now accepts queries and aligned result batches explicitly:

```python
results = sdk.search.many(queries)
fused = sdk.search.fuse_rrf(queries, results)
```

The explicit query list preserves provenance after removing the Outcome wrapper. `None` batches are
skipped, successful empty batches are accepted, and weights remain aligned with every input
position.

## Migration

| Previous generated-program API | Replacement |
| --- | --- |
| `Outcome[T]` | `T | None` |
| `list[Outcome[T]]` | `list[T | None]` |
| `outcome.status == "success"` | `result is not None` |
| `outcome.value` | `result` |
| `outcome.error` | Automatically rendered structured warning |
| `fuse_rrf(search_outcomes, ...)` | `fuse_rrf(queries, search_results, ...)` |

Regenerate programs and deploy matching service and sandbox images. Existing host metrics, stored
execution records, configuration keys, and broker wire operations require no migration.
