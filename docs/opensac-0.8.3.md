# OpenSAC v0.8.3

OpenSAC v0.8.3 simplifies the generated-program SDK around search, content inspection, optional
model transformation, deployment capabilities, persistent workspace artifacts, and bounded stdout.
It removes public usage and output namespaces, moves capability discovery to the SDK top level, and
renames the structured persistence namespace from `state` to `workspace`.

These changes are intentionally breaking. No compatibility aliases, deprecation shims, or parallel
namespaces are provided. Capability contract 15 rejects the removed `session.usage` broker operation;
sandbox contract 14 is unchanged.

## Public SDK surface

Generated programs now use these namespaces:

- `sdk.search` for retrieval and local RRF fusion.
- `sdk.content` for fetching and inspecting admitted sources.
- `sdk.llm` for optional completion and schema-checked extraction.
- `sdk.workspace` for JSON and JSONL artifacts that persist across executions in one live session.
- `sdk.capabilities()` for active contracts, mechanisms, backends, and configured limits.

The SDK surface contains 21 operations: 20 are public and 11 are in the model-core subset.
`sdk.workspace` is a local sandbox helper and does not add a broker capability or wire operation.

## Usage visibility

The generated-program `session.usage()` method and broker `session.usage` operation were removed.
Agent and CLI observations also omit usage counters such as search calls and fetched-document counts.

Host accounting remains intact. `RunUsage`, `ResourceBudget`, policy counters, execution usage,
session budgets, provider token metadata, storage, REST responses, and dashboard metrics continue to
enforce and report resource consumption outside the model-visible SDK.

## Capabilities

Capability discovery is now the top-level `sdk.capabilities()` call. The broker continues to serve
the internal unary `session.capabilities` RPC, so this namespace change does not add or rename a wire
operation.

## Program results

The `sdk.output` namespace and `OutputResource` were removed. Generated programs return bounded,
source-scoped results with ordinary `print(...)` calls and keep larger reusable values in
`sdk.workspace`.

Agent observations ignore legacy `output` and `citations` fields even when a REST execution payload
contains them. Host `ExecResult` fields, storage, dashboard data, and sandbox output-envelope parsing
remain available for host compatibility and internal diagnostics.

## Workspace artifacts

`sdk.workspace` replaces `sdk.state` without changing the structured artifact methods:

```python
sdk.workspace.write_json(path, value)
sdk.workspace.read_json(path)
sdk.workspace.write_jsonl(path, rows)
sdk.workspace.append_jsonl(path, rows)
sdk.workspace.upsert_jsonl(path, rows, key="source")
sdk.workspace.read_jsonl(path)
sdk.workspace.exists(path)
sdk.workspace.list(prefix="")
```

Paths remain relative to the session workspace and cannot escape it. The `state_lost` observation
keeps its existing meaning because it covers the complete live session state, including workspace
files, source admission, and persistent-interpreter lifecycle.

## Migration

| Previous generated-program API | Replacement |
| --- | --- |
| `sdk.session.usage()` | Host REST, storage, or dashboard observability |
| `sdk.session.capabilities()` | `sdk.capabilities()` |
| `sdk.output.submit(...)` | Bounded `print(...)` plus `sdk.workspace` for reusable artifacts |
| `sdk.state.*` | `sdk.workspace.*` |

Regenerate programs and deploy matching v0.8.3 service and sandbox images. Existing host metrics and
stored execution records require no migration. No configuration keys were added or removed.
