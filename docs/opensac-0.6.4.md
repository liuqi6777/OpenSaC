# OpenSAC v0.6.4

OpenSAC v0.6.4 makes the generated-program SDK more regular, discoverable, and strict. The release
separates single-source reads from batch windows, replaces `grep_report` with a coverage-aware
`grep`, publishes shape-aware typing metadata, and makes local validation and JSON persistence
fail before provider work or artifact replacement.

## Breaking SDK changes

- `sdk.content.read(source, ...) -> Record` now accepts exactly one source. Replace
  `sdk.content.read([source], ...)[0]` with `sdk.content.read(source, ...)`.
- Use `sdk.content.read_many(windows) -> list[Record]` for batch reads. Each window has its own
  `source`, `offset`, `limit`, and `max_chars`; rows align by `input_index`, and duplicate sources
  reuse broker fetch work.
- `sdk.content.grep_report(...)` is removed. Use `sdk.content.grep(...)` with explicit `mode="regex"`
  or `mode="literal"` and optional `case_sensitive`.
- Grep reports expose one input-aligned `source_results` row per source. `match_count`,
  `scan_complete`, and `failure` distinguish complete zero-match scans, capped scans, and fetch
  failures.
- `sdk.llm.extract_many` failure rows use `failure` instead of `error`.
- Arguments are validated without string, integer, boolean, or range coercion. Invalid input fails
  locally or at the broker boundary before provider or budget side effects.

## Discoverability, typing, and persistence

- `sdk.session.capabilities()` reports the active sandbox/capability contracts, backend features,
  public limits, optional LLM availability, and mechanisms without exposing secrets.
- `sdk.session.usage()` now separates logical content requests from backend fetches, reports actual
  and reserved resource use, exposes provider attempt/retry metrics, and includes `budget_consumed`
  beside `budget_remaining`.
- The SDK wheel includes `py.typed` and a shape-aware `__init__.pyi` for public methods and records.
- State and output writers use strict JSON: unsupported objects, NaN, and Infinity raise
  `ValueError`. Replacement writes are atomic, and append operations encode the full input before
  changing an artifact.

## Compatibility

The sandbox contract is `10` and the capability contract is `9`. This release is intentionally
incompatible with the v0.6.3 sandbox RPC surface; deploy matching v0.6.4 service and sandbox images.

## Migration example

```python
from opensac_sdk import sdk

report = sdk.content.grep(
    sources,
    r"born in \d{4}",
    mode="regex",
    case_sensitive=False,
    context=2,
)

failures = [row for row in report.source_results if row.failure is not None]
if report.matches:
    match = report.matches[0]
    passage = sdk.content.read(
        match.source,
        offset=max(1, match.line - 5),
        limit=11,
    )
```

See the complete SDK API reference in [English](sdk-reference.md) or
[Chinese](sdk-reference.zh-CN.md).
