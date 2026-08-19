# Workspace-backed Search-as-Code research

Read this reference only when research must continue across multiple OpenSAC program calls. The
workspace is the program's durable notebook; stdout is only the control model's bounded view. Each
code block is one stage, not one program to paste in full. Run only the next useful stage and adapt
every placeholder. Searching first and choosing a verification strategy after inspecting candidates
is valid.

## Contents

- [Workspace contract](#workspace-contract)
- [1. Search or extend the candidate pool](#1-search-or-extend-the-candidate-pool)
- [2. Verify one missing constraint](#2-verify-one-missing-constraint)
- [3. Inspect workspace after an uncertain failure](#3-inspect-workspace-after-an-uncertain-failure)
- [4. Submit a complete ledger](#4-submit-a-complete-ledger)

## Workspace contract

Keep one task-derived `runs/<research_id>/` namespace. Observations show artifact paths, not file
contents; every later program must explicitly load the files it needs.

| Artifact | Durable decision | Update rule |
| --- | --- | --- |
| `manifest.json` | Task, stable requirements, source policy | Write once per namespace |
| `pool.jsonl` | Bounded candidate metadata and sources | Merge, rerank, then prune |
| `evidence.json` | Verified passages and locators | Keep matching fingerprints |
| `attempts.json` | Sources tried for each matching rule | Save before capability calls |

For every stage:

- Derive the ID from the exact task, stable requirements, and source policy—not queries or regexes.
- Start with `sdk.state.list(f"{root}/")`, then read the artifacts the stage needs.
- Save progress before printing `NEXT:` or exiting. Python variables do not survive calls.
- Keep pools bounded and skip attempted `(constraint, source)` pairs.
- Treat stored sources and locators as valid only in the same live broker session.
- Use bounded `print` output for progress; use `sdk.output.submit` for the final result.

## 1. Search or extend the candidate pool

This stage also initializes the namespace. Reuse its printed `research_id` later. Run it again only
with useful new queries when the pool does not cover a missing constraint.

```python
import hashlib
import json

from opensac_sdk import sdk

manifest = {
    "task": "Identify the target entity and verify the requested phrase and year.",
    "requirements": {
        "phrase": "Attribute the target phrase to the entity.",
        "year": "Relate the target event to 1998 or 1999.",
    },
    "source_policy": {"preferred": "Primary sources when available."},
}
identity = json.dumps(manifest, ensure_ascii=True, sort_keys=True)
research_id = hashlib.sha256(identity.encode()).hexdigest()[:12]
root = f"runs/{research_id}"
manifest_path = f"{root}/manifest.json"
pool_path = f"{root}/pool.jsonl"
artifacts = set(sdk.state.list(f"{root}/"))
if manifest_path not in artifacts:
    sdk.state.write_json(manifest_path, manifest)

queries = ['"exact phrase" entity', "entity alternate wording", "rare clue organization"]
batches = sdk.search.many(queries, limit_per_query=10, concurrency=6)
fusion = sdk.search.fuse_rrf(batches, k=60)
rank_now = {candidate.source: candidate.fused_rank for candidate in fusion}
new_rows = [
    {
        "source": candidate.source,
        "title": candidate.title,
        "domain": candidate.domain,
        "snippet": candidate.snippet[:400],
        "score": candidate.fused_score,
    }
    for candidate in fusion
]
sdk.state.merge_jsonl(pool_path, new_rows)
pool = [dict(row) for row in sdk.state.read_jsonl(pool_path)]
pool.sort(
    key=lambda row: (
        0 if row["source"] in rank_now else 1,
        rank_now.get(row["source"], 1_000_000),
        -float(row.get("score") or 0.0),
    )
)
sdk.state.write_jsonl(pool_path, pool[:200])

print(f"WORKSPACE research={research_id} pool={min(len(pool), 200)}")
for row in pool[:5]:
    print(f"CANDIDATE {row.get('domain') or '-'} | {row.get('title') or '(untitled)'}")
print("NEXT: inspect candidates, verify a constraint, or refine the queries")
```

## 2. Verify one missing constraint

Adapt `name`, `requirement`, and `pattern`, then run this stage for one missing constraint. Its
fingerprint invalidates only that constraint when the rule changes. Attempted sources are saved before
content calls so the next stage does not silently rescan them.

```python
import hashlib
import json
import re

from opensac_sdk import sdk

research_id = "copy-the-task-derived-id"
root = f"runs/{research_id}"
name = "phrase"
requirement = "Attribute the target phrase to the entity."
pattern = r"(target phrase|other spelling)"
pool_path = f"{root}/pool.jsonl"
evidence_path = f"{root}/evidence.json"
attempts_path = f"{root}/attempts.json"
artifacts = set(sdk.state.list(f"{root}/"))
pool = sdk.state.read_jsonl(pool_path) if pool_path in artifacts else []
evidence = dict(sdk.state.read_json(evidence_path)) if evidence_path in artifacts else {}
attempts = dict(sdk.state.read_json(attempts_path)) if attempts_path in artifacts else {}

rule = {"requirement": requirement, "pattern": pattern}
fingerprint = hashlib.sha256(json.dumps(rule, sort_keys=True).encode()).hexdigest()
if evidence.get(name, {}).get("fingerprint") != fingerprint:
    evidence.pop(name, None)
saved = attempts.get(name, {})
attempted = set(saved.get("sources", [])) if saved.get("fingerprint") == fingerprint else set()
sources = [row.source for row in pool if row.source not in attempted][:40]

if sources:
    attempted.update(sources)
    attempts[name] = {"fingerprint": fingerprint, "sources": sorted(attempted)}
    sdk.state.write_json(attempts_path, attempts)
    report = sdk.content.grep_report(sources, pattern, context=2)
    for match in report.matches[:6]:
        passage = sdk.content.read(
            [match.source], offset=max(match.line - 10, 1), limit=40, max_chars=16_000
        )[0]
        if (
            passage.failure is None
            and passage.locator is not None
            and re.search(pattern, passage.text, re.IGNORECASE)
        ):
            evidence[name] = {
                "fingerprint": fingerprint,
                "requirement": requirement,
                "source": passage.source,
                "text": passage.text,
                "locator": passage.locator,
            }
            sdk.state.write_json(evidence_path, evidence)
            break

print(f"constraint={name} verified={name in evidence} tried={len(attempted)}")
print("NEXT: verify another constraint, search for new candidates, or submit")
```

## 3. Inspect workspace after an uncertain failure

Use this read-only probe after an adapter failure whose execution outcome is unknown. After explicit
`state_lost`, start a clean generation instead.

```python
from opensac_sdk import sdk

research_id = "copy-the-task-derived-id"
root = f"runs/{research_id}"
artifacts = sdk.state.list(f"{root}/")
pool_path = f"{root}/pool.jsonl"
evidence_path = f"{root}/evidence.json"
pool_count = len(sdk.state.read_jsonl(pool_path)) if sdk.state.exists(pool_path) else 0
evidence = sdk.state.read_json(evidence_path) if sdk.state.exists(evidence_path) else {}
usage = sdk.session.usage()

print(
    f"WORKSPACE research={research_id} artifacts={artifacts} "
    f"pool={pool_count} evidence={sorted(evidence)} terminal={usage['terminal_reason']!r}"
)
print("NEXT: resume only the missing constraint or stage")
```

## 4. Submit a complete ledger

Use `submit` only when every requirement has verified evidence. If anything is missing, print the
next action instead of a final-looking answer.

```python
from opensac_sdk import sdk

research_id = "copy-the-task-derived-id"
root = f"runs/{research_id}"
required = ["phrase", "year"]
artifacts = set(sdk.state.list(f"{root}/"))
evidence_path = f"{root}/evidence.json"
evidence = sdk.state.read_json(evidence_path) if evidence_path in artifacts else {}
missing = [name for name in required if name not in evidence]

if missing:
    print(f"NEXT: verify missing constraints {missing}")
else:
    sdk.output.submit(
        {
            "research_id": research_id,
            "evidence": [
                {
                    "constraint": name,
                    "requirement": evidence[name]["requirement"],
                    "source": evidence[name]["source"],
                    "text": evidence[name]["text"][:2_000],
                }
                for name in required
            ],
        },
        citations=[
            {"locator": evidence[name]["locator"]}
            for name in required
        ],
    )
```
