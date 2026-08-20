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

artifacts = set(sdk.state.list(f"{root}/"))
sdk.state.write_json(manifest_path, research_manifest)
pool = {
    row.source: dict(row)
    for row in (sdk.state.read_jsonl(pool_path) if pool_path in artifacts else [])
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
for batch in batches:
    if batch.failure is not None:
        print(f"query failed: {batch.query} code={batch.failure.code}")

leader_sources = []
for batch in batches:
    if batch.failure is not None:
        continue
    for hit in batch.hits[:2]:
        if hit.source not in leader_sources:
            leader_sources.append(hit.source)

current_rank = {candidate.source: candidate.fused_rank for candidate in fusion}
for candidate in fusion:
    row = pool.setdefault(
        candidate.source,
        {
            "source": candidate.source,
            "title": "",
            "domain": None,
            "date": None,
            "snippet": "",
            "score": 0.0,
        },
    )
    row["title"] = candidate.title or row.get("title", "")
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
        0 if row["source"] in current_rank else 1,
        current_rank.get(row["source"], 1_000_000),
        -float(row.get("score") or 0.0),
        row["source"],
    ),
)

selected_sources = []
for source in leader_sources:
    if source in pool and source not in selected_sources and len(selected_sources) < POOL_LIMIT:
        selected_sources.append(source)
for row in ordered:
    if row["source"] not in selected_sources and len(selected_sources) < POOL_LIMIT:
        selected_sources.append(row["source"])
selected = set(selected_sources)
bounded_pool = [row for row in ordered if row["source"] in selected]
sdk.state.write_jsonl(pool_path, bounded_pool)
ordered_sources = [row["source"] for row in bounded_pool]
pool_by_source = {row["source"]: row for row in bounded_pool}

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
loaded_evidence = sdk.state.read_json(evidence_path) if evidence_path in artifacts else {}
evidence = {
    name: dict(row)
    for name, row in loaded_evidence.items()
    if name in fingerprints and row.get("fingerprint") == fingerprints[name]
}
loaded_attempts = sdk.state.read_json(attempts_path) if attempts_path in artifacts else {}
attempted = {}
for name in constraints:
    row = loaded_attempts.get(name, {})
    attempted[name] = (
        set(row.get("sources", [])) if row.get("fingerprint") == fingerprints[name] else set()
    )

for name, spec in constraints.items():
    if name in evidence:
        print(f"{name}: verified in ledger")
        continue

    pattern = spec["pattern"]
    compiled = re.compile(pattern, re.IGNORECASE)
    available = [source for source in ordered_sources if source not in attempted[name]]
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
            if match.source in seen_matches:
                continue
            seen_matches.add(match.source)
            reads_for_constraint += 1
            try:
                passage = sdk.content.read(
                    [match.source],
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
            if not passage.text.strip() or not compiled.search(passage.text):
                continue

            evidence[name] = {
                "fingerprint": fingerprints[name],
                "requirement": spec["requirement"],
                "source": passage.source,
                "text": passage.text,
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
        name: {"fingerprint": fingerprints[name], "sources": sorted(sources)}
        for name, sources in attempted.items()
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
                    "source": row["source"],
                    "title": pool_by_source.get(row["source"], {}).get("title"),
                    "text": row["text"][:2_000],
                }
                for name, row in evidence.items()
            ],
        },
        citations=list(dict.fromkeys(row["source"] for row in evidence.values())),
    )
