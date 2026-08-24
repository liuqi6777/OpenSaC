# REPL namespace, checkpoint, and recovery

The persistent interpreter is the working notebook. Python globals are cheap working state;
`sdk.state` is a deliberate recovery checkpoint. Keep temporary candidates and helper functions in
memory, and serialize only expensive results or verified evidence at meaningful phase boundaries.

## Contents

- [Namespace contract](#namespace-contract)
- [1. Build live working state](#1-build-live-working-state)
- [2. Verify incrementally](#2-verify-incrementally)
- [3. Inspect after an uncertain failure](#3-inspect-after-an-uncertain-failure)
- [4. Checkpoint and submit](#4-checkpoint-and-submit)
- [Interpreter loss](#interpreter-loss)

## Namespace contract

- Use semantic names such as `candidate_pool`, `passage_report`, and `verified_evidence`.
- End review cells with `NEXT:` and explicitly name every global the next cell should reuse.
- Overwrite or `del` objects that no longer reflect the current strategy.
- Ordinary Python exceptions do not clear assignments completed earlier in the same cell.
- Checkpoint expensive capability results and verified evidence at phase boundaries, not after every
  mutation. Treat `sdk.state` as recovery state rather than a mirror of the namespace.
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
    search_batches = sdk.search.many(queries, limit_per_query=10, concurrency=4)
except BrokerError as error:
    print(f"ERROR: search code={error.code} retryable={error.retryable}")
else:
    candidate_pool = sdk.search.fuse_rrf(search_batches, k=60, limit=40)
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
        if passage.failure is None and re.search(pattern, passage.text, re.IGNORECASE):
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

## 4. Checkpoint and submit

Checkpoint only at a stable phase boundary. Convert SDK records to plain dictionaries at that
boundary so recovery does not depend on in-memory record classes.

```python
from opensac_sdk import sdk

required_constraints = set(research_manifest["requirements"])
missing_constraints = sorted(required_constraints - verified_evidence.keys())
checkpoint = {
    "manifest": research_manifest,
    "candidates": [dict(item) for item in candidate_pool[:40]],
    "evidence": verified_evidence,
}
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
The adapter discards that session; the next invocation starts a clean one. Restore only an existing
phase checkpoint, re-admit local sources, and redo operations absent from the checkpoint. Never infer
completion merely because a capability was present in the lost cell's source code.
