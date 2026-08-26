# REPL namespace and optional recovery checkpoints

This reference shows one possible use of the persistent namespace and `sdk.state`; it is not a
required state layout or cell sequence. Python globals are live working memory. Files are a separate,
optional recovery mechanism when the deployment enables persistence.

## Contents

- [Runtime semantics](#runtime-semantics)
- [1. Build live working state](#1-build-live-working-state)
- [2. Verify incrementally](#2-verify-incrementally)
- [3. Inspect after an uncertain failure](#3-inspect-after-an-uncertain-failure)
- [4. Optionally checkpoint and submit](#4-optionally-checkpoint-and-submit)
- [Interpreter loss](#interpreter-loss)

## Runtime semantics

- Choose global names and structures that fit the task. Names in these examples are illustrative.
- Reuse live objects, recompute them, or overwrite and delete them as useful. The example `NEXT:`
  lines are optional stdout conventions, not a cell protocol.
- Ordinary Python exceptions do not clear assignments completed earlier in the same cell.
- Choose whether to checkpoint from recomputation cost and failure risk. No checkpoint boundary or
  schema is required; avoid mirroring the entire namespace by default.
- Confirm `sdk.session.capabilities()["mechanisms"]["persistence"]` before relying on a checkpoint.
  With persistence disabled, `sdk.state` uses the current temporary workspace and cannot recover a
  later cell or a lost interpreter.
- If an adapter failure leaves execution uncertain, first inspect globals and
  `sdk.session.usage()` without making another external call.
- Public web URLs remain reusable after recovery. Local document IDs must be re-admitted by search
  after interpreter or session loss.

## 1. Build live working state

```python
import hashlib
import json

from opensac_sdk import BrokerError, sdk

research_manifest = {
    "task": "Identify the target and verify the requested phrase and year.",
    "requirements": ["phrase", "year"],
    "source_policy": "Prefer primary sources when available.",
}
manifest_text = json.dumps(research_manifest, sort_keys=True)
research_id = hashlib.sha256(manifest_text.encode()).hexdigest()[:12]
checkpoint_root = f"runs/{research_id}"
queries = ['"exact phrase" entity', "entity alternate wording", "rare clue organization"]

try:
    search_report = sdk.search.many(queries, limit_per_query=10, concurrency=4)
except BrokerError as error:
    print(f"ERROR: search code={error.code} retryable={error.retryable}")
else:
    candidate_pool = sdk.search.fuse_rrf(search_report, k=60, limit=40)
    verified_evidence = {}
    print(f"candidates={len(candidate_pool)} research={research_id}")
    print(
        "NEXT: inspect and verify one requirement; reuse candidate_pool, "
        "verified_evidence, research_manifest, checkpoint_root"
    )
```

## 2. Verify incrementally

Adapt one rule at a time. `verified_evidence` retains successful checks across later cells.

```python
import re

from opensac_sdk import BrokerError, sdk

constraint_name = "phrase"
pattern = r"(target phrase|other spelling)"
candidate_sources = [item.source for item in candidate_pool[:30]]

try:
    grep_report = sdk.content.grep(candidate_sources, pattern, context=2)
except BrokerError as error:
    print(f"ERROR: grep code={error.code} retryable={error.retryable}")
else:
    for match in grep_report.matches[:6]:
        passage = sdk.content.read(
            match.source, offset=max(match.line - 10, 1), limit=40, max_chars=16_000
        )
        if re.search(pattern, passage.text, re.IGNORECASE):
            verified_evidence[constraint_name] = {
                "source": passage.source,
                "text": passage.text,
                "pattern": pattern,
            }
            break
    print(f"verified={sorted(verified_evidence)}")
    print(
        "NEXT: verify another requirement or checkpoint; reuse candidate_pool, "
        "verified_evidence, checkpoint_root"
    )
```

## 3. Inspect after an uncertain failure

This probe performs no search, content, extraction, state write, or output submission. Run it before
deciding whether any capability call needs to be retried.

```python
from opensac_sdk import sdk

important_names = [
    "research_id",
    "candidate_pool",
    "grep_report",
    "verified_evidence",
    "checkpoint_root",
]
present_names = [name for name in important_names if name in globals()]
usage_snapshot = sdk.session.usage()
verified_names = sorted(verified_evidence) if "verified_evidence" in globals() else []

print(
    f"RECOVERY globals={present_names} verified={verified_names} "
    f"terminal={usage_snapshot.get('terminal_reason')!r}"
)
print(
    "NEXT: resume only the missing operation; reuse present_names, "
    "usage_snapshot, verified_evidence when present"
)
```

## 4. Optionally checkpoint and submit

This example saves a compact recovery artifact only when filesystem persistence is enabled. Adapt or
omit the artifact, schema, and timing. Converting SDK records to plain dictionaries keeps a saved
checkpoint independent of in-memory record classes.

```python
from opensac_sdk import sdk

required_constraints = set(research_manifest["requirements"])
missing_constraints = sorted(required_constraints - verified_evidence.keys())
checkpoint = {
    "manifest": research_manifest,
    "candidates": [dict(item) for item in candidate_pool[:40]],
    "evidence": verified_evidence,
}
capabilities = sdk.session.capabilities()
persistence_enabled = bool(capabilities["mechanisms"]["persistence"])
if persistence_enabled:
    sdk.state.write_json(f"{checkpoint_root}/checkpoint.json", checkpoint)

if missing_constraints:
    print(
        f"NEXT: verify missing constraints {missing_constraints}; "
        "reuse candidate_pool, verified_evidence, checkpoint_root"
    )
else:
    sdk.output.submit(
        {"research_id": research_id, "evidence": verified_evidence},
        citations=list(dict.fromkeys(row["source"] for row in verified_evidence.values())),
    )
```

## Interpreter loss

An observation with `interpreter_state=lost` or `state_lost` means the cell is never replayed.
The adapter discards that session; the next invocation starts a clean one. Restore a trustworthy
checkpoint if one exists, re-admit local sources, and redo any work that evidence does not show as
complete. Never infer completion merely because a capability appeared in the lost cell's source.
