# OpenSAC v0.8.1

OpenSAC v0.8.1 is an intentional breaking pre-1.0 SDK simplification. It removes public batch
methods that did not provide meaningful composition semantics, keeps `search.many` for multi-query
retrieval and RRF, and makes single-operation errors explicit through `BrokerError`.

There is no compatibility alias, deprecation shim, or parallel namespace. Capability contract 13
and sandbox contract 14 reject mixed 0.8.0/0.8.1 SDK, broker, and sandbox deployments.

## Public SDK surface

- Search keeps `search`, `search.many`, and local `search.fuse_rrf`.
- Content now exposes `fetch`, `read`, `grep`, and `passages`.
- LLM now exposes single-item `complete` and `extract`.
- State renames `merge_jsonl` to `upsert_jsonl`.
- Session keeps `usage` and `capabilities`; output keeps `submit`.

Removed methods are `content.get_many`, `content.read_many`, `llm.complete_many`, and
`llm.extract_many`. Callers loop over the corresponding single method and decide how to retain
successes after each `BrokerError`. No `fetch_many`, `grep_many`, or `passages_many` was added.

## Content cursors

`content.read` now accepts 1-based `start_line`, 0-based `start_character`, `line_count`, and
`max_chars`. Provider document metadata remains in `metadata`; coordinates are reported separately
in `window`. `window.next` is an exact cursor that continues without losing a newline or characters,
including a `max_chars` cut inside one long line.

`content.grep` now accepts `pattern` first and keyword-only `sources`. It returns one outcome per
source; each outcome owns its character-spanned matches and `next_start_line`, allowing capped
scans to resume without duplicates or omissions.

## Aligned partial outcomes

`search.many` and `content.grep` return input-aligned lists instead of top-level reports with
parallel result and failure collections. Each row has `status == "success"` or a bounded,
human-readable failure string. Failed search rows have empty `hits`; failed grep rows have empty
`matches`, `title=None`, and `next_start_line=None`. Callers must not parse failure strings.

The broker wire reports remain structured and retain `input_index`, error codes, retry metadata,
and provider context. The SDK records those structured diagnostics before presenting the simplified
outcome lists, so host observability is unchanged.

## Extraction and quota

`llm.extract` accepts one item and returns the schema-validated JSON object directly. Provider,
invalid JSON, non-object output, schema mismatch, repair exhaustion, and quota failure are all
top-level `BrokerError` values with specific codes and attempt counts. Every initial or repair model
attempt reserves one LLM call before dispatch.

`ResourceBudget` remains enforced. The public `session.usage()` response is reduced to strategy
state: capability counters, reserved output tokens, sandbox/workspace consumption, remaining
budgets, and terminal reason. Provider retries, cache/coalescing behavior, queueing, and actual token
metrics remain available to host observability.

## Renames

| 0.8.0 | 0.8.1 |
| --- | --- |
| `domains` | `include_domains` |
| `search.many(..., limit_per_query=...)` | `limit=...` |
| fusion `batch_index` | `input_index` |
| read `offset` / `limit` | `start_line` / `line_count` plus `start_character` |
| grep `context` / `max_matches_per_source` | `context_lines` / `limit_per_source` |
| passages `max_per_source` | `limit_per_source` |
| `state.merge_jsonl` | `state.upsert_jsonl` |
| `output.submit(output=...)` | `output.submit(value=...)` |

The SDK runtime, type stubs, broker dispatch, capability manifests, sandbox image, examples,
Search-as-Code skills, agent prompt, and reference documentation use the new names atomically.

## Deployment

Deploy matching `0.8.1` service and sandbox images. Generated programs built for the old surface are
expected to fail and must be regenerated or migrated. Tagging and publishing remain separate release
steps described in `docs/releasing.md`.
