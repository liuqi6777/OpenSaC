# OpenSAC v0.9.0

OpenSAC v0.9.0 removes the public `sdk.workspace` namespace. Generated programs now use standard
Python file I/O to save and load artifacts in the session working directory. File retention remains
owned by the runtime; no new storage service or broker operation is introduced.

This is a breaking SDK change. Host and SDK versions advance together to 0.9.0. Capability
contract 15 and sandbox contract 14 remain unchanged because the broker RPC boundary is unchanged.

## Migrate generated programs

Replace `sdk.workspace.write_json` and `read_json` with `json` and `pathlib`. Each `program` call
starts with fresh Python variables, so reconstruct the relative path in the next call:

```python
# First call
import json
from pathlib import Path

path = Path("artifacts/checkpoint.json")
path.parent.mkdir(parents=True, exist_ok=True)
pending = path.with_suffix(".tmp")
pending.write_text(
    json.dumps({"sources": []}, ensure_ascii=False, allow_nan=False), encoding="utf-8"
)
pending.replace(path)
```

```python
# Later call in the same session
import json
from pathlib import Path

state = json.loads(Path("artifacts/checkpoint.json").read_text(encoding="utf-8"))
print(state["sources"])
```

- SDK records are dictionaries and can be serialized directly. JSON reads return plain dictionaries;
  replace attribute reads such as `row.source` with `row["source"]` after loading saved data.
- For JSONL, use one JSON object per line. Replace `append_jsonl` with a file opened in append mode;
  replace `upsert_jsonl` by loading rows, merging by the chosen key, and writing the updated pool.
- Replace `exists` and `list` with `Path.is_file` and directory traversal scoped to your artifacts.
  `.opensac-*` files belong to the runtime and should not be modified.
- Temporary-file replacement protects individual checkpoints from partial writes. It does not
  provide transactions across multiple artifacts.

Existing JSON and JSONL files remain readable. Update generated programs, saved snippets, and
installed research skills with the matching release. There is no compatibility alias for
`sdk.workspace`.

## Execution modes and lifecycle

The default `program` mode still starts a fresh process per call. The experimental
`persistent_interpreter` variant still retains Python variables, functions, and imports across
calls. Both modes use ordinary files; `mechanisms.persistence` continues to control file retention
independently of live interpreter memory.

Session isolation, budgets, workspace cleanup, document caches, local source IDs, and broker
permissions are unchanged. Files remain session-bound and are not guaranteed to survive a
host-reported `state_lost`. This release does not add recovery across hosts or service restarts.
