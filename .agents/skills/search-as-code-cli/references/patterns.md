# Search-as-Code patterns

Read the canonical pattern before a multi-turn or multi-constraint investigation. Adapt every
placeholder to the actual task; do not rerun unchanged queries after the attempted ledger stops
advancing.

## Contents

- [Canonical multi-turn pattern](#canonical-multi-turn-pattern)
- [Why the state is shaped this way](#why-the-state-is-shaped-this-way)
- [Checked semantic extraction](#checked-semantic-extraction)

## Canonical multi-turn pattern

```python
import hashlib
import json
import re

from opensac_sdk import BrokerError, sdk

# Include the exact user task here. It separates unrelated research in one conversation.
task = "Identify the target entity and verify the requested phrase and year."
constraints = {
    "phrase": {
        "requirement": "The passage explicitly attributes the target phrase to the entity.",
        "pattern": r"(target phrase|other spelling)",
    },
    "year": {
        "requirement": "The passage explicitly relates the target event to 1998 or 1999.",
        "pattern": r"\b(1998|1999)\b",
    },
}
source_policy = {
    "preferred_sources": "Primary sources when available.",
    "corroboration": "Corroborate disputed claims with independent sources.",
}
research_manifest = {
    "task": task,
    "requirements": {name: spec["requirement"] for name, spec in constraints.items()},
    "source_policy": source_policy,
}
manifest_text = json.dumps(research_manifest, ensure_ascii=True, sort_keys=True)
research_id = hashlib.sha256(manifest_text.encode()).hexdigest()[:12]
root = f"runs/{research_id}"
pool_path = f"{root}/pool.jsonl"
evidence_path = f"{root}/evidence.json"
attempts_path = f"{root}/attempts.json"
manifest_path = f"{root}/manifest.json"

POOL_LIMIT = 200
CONTENT_BATCH = 40
SCAN_LIMIT_PER_CONSTRAINT = 80
READ_LIMIT_PER_CONSTRAINT = 6

sdk.state.write_json(manifest_path, research_manifest)
pool = {
    row.ref: dict(row)
    for row in (sdk.state.read_jsonl(pool_path) if sdk.state.exists(pool_path) else [])
}

# Use 2-4 focused variants for a known entity; expand only when discovery is ambiguous.
queries = list(
    dict.fromkeys(
        [
            '"exact phrase" narrowing words',
            "entity name relation alternate wording",
            "rare clue source title or organization",
        ]
    )
)
try:
    batches = sdk.search.many(queries, limit_per_query=10, concurrency=6)
except BrokerError as error:
    print(f"search failed: code={error.code} retryable={error.retryable} attempts={error.attempts}")
    batches = []

fusion = sdk.search.fuse_rrf(batches, k=60)
for item in fusion.batch_errors:
    print(f"query failed: {item.query} code={item.failure.code}")

leader_refs = []
for batch in batches:
    if batch.failure is not None:
        continue
    for hit in batch.hits[:2]:
        if hit.ref not in leader_refs:
            leader_refs.append(hit.ref)

current_rank = {candidate.ref: candidate.fused_rank for candidate in fusion.candidates}
for candidate in fusion.candidates:
    row = pool.setdefault(
        candidate.ref,
        {
            "ref": candidate.ref,
            "title": "",
            "url": None,
            "domain": None,
            "date": None,
            "snippet": "",
            "score": 0.0,
        },
    )
    row["title"] = candidate.title or row.get("title", "")
    row["url"] = candidate.url or row.get("url")
    row["domain"] = candidate.domain or row.get("domain")
    row["date"] = candidate.date or row.get("date")
    if candidate.snippet:
        row["snippet"] = candidate.snippet[:400].replace("\n", " ")
    row["score"] = max(float(row.get("score") or 0.0), candidate.fused_score)

# Merge first, then prune. Current-stage rank wins; historical score is only a fallback.
sdk.state.merge_jsonl(pool_path, list(pool.values()))
merged = [dict(row) for row in sdk.state.read_jsonl(pool_path)]
ordered = sorted(
    merged,
    key=lambda row: (
        0 if row["ref"] in current_rank else 1,
        current_rank.get(row["ref"], 1_000_000),
        -float(row.get("score") or 0.0),
        row["ref"],
    ),
)

selected_refs = []
for ref in leader_refs:
    if ref in pool and ref not in selected_refs and len(selected_refs) < POOL_LIMIT:
        selected_refs.append(ref)
for row in ordered:
    if row["ref"] not in selected_refs and len(selected_refs) < POOL_LIMIT:
        selected_refs.append(row["ref"])
selected = set(selected_refs)
bounded_pool = [row for row in ordered if row["ref"] in selected]
sdk.state.write_jsonl(pool_path, bounded_pool)
ordered_refs = [row["ref"] for row in bounded_pool]
pool_by_ref = {row["ref"]: row for row in bounded_pool}

print(f"research={research_id} pool={len(bounded_pool)} new={len(current_rank)}")
for row in bounded_pool[:5]:
    print(
        f"source: {row.get('date') or '-'} {row.get('domain') or '-'} "
        f"{row.get('title') or '(untitled)'}"
    )

fingerprints = {
    name: hashlib.sha256(json.dumps(spec, ensure_ascii=True, sort_keys=True).encode()).hexdigest()
    for name, spec in constraints.items()
}
loaded_evidence = sdk.state.read_json(evidence_path) if sdk.state.exists(evidence_path) else {}
evidence = {
    name: dict(row)
    for name, row in loaded_evidence.items()
    if name in fingerprints and row.get("fingerprint") == fingerprints[name]
}
loaded_attempts = sdk.state.read_json(attempts_path) if sdk.state.exists(attempts_path) else {}
attempted = {}
for name in constraints:
    row = loaded_attempts.get(name, {})
    attempted[name] = (
        set(row.get("refs", [])) if row.get("fingerprint") == fingerprints[name] else set()
    )

for name, spec in constraints.items():
    if name in evidence:
        print(f"{name}: verified in ledger")
        continue

    pattern = spec["pattern"]
    compiled = re.compile(pattern, re.IGNORECASE)
    available = [ref for ref in ordered_refs if ref not in attempted[name]]
    available = available[:SCAN_LIMIT_PER_CONSTRAINT]
    if not available:
        print(f"{name}: no untried candidates; change the queries")
        continue

    reads_for_constraint = 0
    for start in range(0, len(available), CONTENT_BATCH):
        chunk = available[start : start + CONTENT_BATCH]
        attempted[name].update(chunk)
        try:
            report = sdk.content.grep_report(chunk, pattern, context=2)
        except BrokerError as error:
            print(f"grep failed: {name} code={error.code} retryable={error.retryable}")
            break

        for failed in report.failures:
            print(
                f"fetch failed: {name} input={failed.input_index} "
                f"code={failed.failure.code} attempts={failed.failure.attempts}"
            )

        seen_matches = set()
        for match in report.matches:
            if reads_for_constraint >= READ_LIMIT_PER_CONSTRAINT:
                break
            if match.ref in seen_matches:
                continue
            seen_matches.add(match.ref)
            reads_for_constraint += 1
            try:
                passage = sdk.content.read(
                    [match.ref],
                    offset=max(match.line - 10, 1),
                    limit=40,
                    max_chars=16_000,
                )[0]
            except BrokerError as error:
                print(f"read failed: {name} code={error.code}")
                continue
            if passage.failure is not None:
                print(f"read failed: {name} code={passage.failure.code}")
                continue
            if (
                not passage.text.strip()
                or passage.locator is None
                or not compiled.search(passage.text)
            ):
                if passage.locator_error is not None:
                    print(f"locator unavailable: {name} code={passage.locator_error.code}")
                continue

            evidence[name] = {
                "fingerprint": fingerprints[name],
                "requirement": spec["requirement"],
                "ref": passage.ref,
                "text": passage.text,
                "locator": passage.locator.model_dump(mode="json"),
            }
            print(f"{name}: verified")
            break

        if name in evidence or reads_for_constraint >= READ_LIMIT_PER_CONSTRAINT:
            break

if evidence:
    sdk.state.write_json(evidence_path, evidence)
sdk.state.write_json(
    attempts_path,
    {
        name: {"fingerprint": fingerprints[name], "refs": sorted(refs)}
        for name, refs in attempted.items()
    },
)

missing = [name for name in constraints if name not in evidence]
print("unsupported:", missing or "none")

if evidence and not missing:
    sdk.output.submit(
        {
            "research_id": research_id,
            "evidence": [
                {
                    "constraint": name,
                    "requirement": row["requirement"],
                    "ref": row["ref"],
                    "title": pool_by_ref.get(row["ref"], {}).get("title"),
                    "url": pool_by_ref.get(row["ref"], {}).get("url"),
                    "text": row["text"][:2_000],
                }
                for name, row in evidence.items()
            ],
        },
        citations=[{"ref": row["ref"], "locator": row["locator"]} for row in evidence.values()],
    )
```

## Why the state is shaped this way

- Hash the exact task, stable requirements, and source policy, not queries or matching regexes.
  Follow-up queries and regex refinements should enrich the same investigation. The per-constraint
  fingerprint invalidates stale evidence and attempted refs after a matching-rule change, while a
  changed task, requirement, or source policy gets a new namespace.
- Keep current-stage candidates ahead of historical ones because RRF scores from differently sized
  query sets are not directly comparable.
- Preserve two leaders per query before pruning so consensus cannot hide a rare exact-clue hit.
- Cap content batches below the deployment maximum. Advance across turns with the attempted ledger
  instead of rescanning the same documents.
- Cap passage reads separately so a high-frequency pattern cannot consume the stage by itself.
- Store the complete locatable passage in the ledger but submit only a compact excerpt. The exact
  passage remains bound to the submitted locator.

## Checked semantic extraction

Use extraction only when regex or deterministic parsing cannot verify the relationship. A valid
JSON result is not necessarily a supported claim, so require an evidence quote and check it against
the original passage.

```python
schema = {
    "type": "object",
    "properties": {
        "supported": {"type": "boolean"},
        "value": {"type": ["string", "null"]},
        "evidence_quote": {"type": ["string", "null"]},
    },
    "required": ["supported", "value", "evidence_quote"],
    "additionalProperties": False,
}
items = [{"ref": passage.ref, "text": passage.text} for passage in passages]
results = sdk.llm.extract_many(
    items,
    instruction="Extract the requested relation only when the passage states it explicitly.",
    schema=schema,
    concurrency=4,
    repair_attempts=1,
)

checked = []
for item, result in zip(items, results, strict=True):
    if result.error is not None:
        print(result.index, result.error.code, result.error.retryable)
        continue
    quote = result.data.get("evidence_quote") if result.data else None
    if result.data.get("supported") and quote and quote in item["text"]:
        checked.append({"ref": item["ref"], "value": result.data.get("value"), "quote": quote})
```

Keep the original passage locator with every accepted extraction. If the pipeline model is not
configured or the checked result is inconclusive, fall back to deterministic evidence gathering
or report the constraint as unsupported.
