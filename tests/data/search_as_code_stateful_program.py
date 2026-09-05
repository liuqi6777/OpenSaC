import hashlib
import json
import re
from pathlib import Path

from opensac_sdk import sdk

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
root = Path("runs") / research_id
root.mkdir(parents=True, exist_ok=True)
pool_path = root / "pool.jsonl"
evidence_path = root / "evidence.json"
attempts_path = root / "attempts.json"
manifest_path = root / "manifest.json"

POOL_LIMIT = 200
CONTENT_BATCH = 40
SCAN_LIMIT_PER_CONSTRAINT = 80
READ_LIMIT_PER_CONSTRAINT = 6

manifest_path.write_text(json.dumps(research_manifest, ensure_ascii=False), encoding="utf-8")
pool = {}
if pool_path.is_file():
    for line in pool_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            pool[row["source"]] = row

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
search_results = sdk.search.many(queries, limit=10, concurrency=6)

fusion = sdk.search.fuse_rrf(queries, search_results, k=60)
leader_sources = []
for result in search_results:
    if result is None:
        continue
    for hit in result[:2]:
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

# The pool already merges by source. Current-stage rank wins over historical score.
ordered = sorted(
    pool.values(),
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
# Replace complete checkpoints atomically so interruption does not leave partial JSON.
pool_pending = pool_path.with_suffix(".tmp")
pool_pending.write_text(
    "".join(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n" for row in bounded_pool),
    encoding="utf-8",
)
pool_pending.replace(pool_path)
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
loaded_evidence = (
    json.loads(evidence_path.read_text(encoding="utf-8")) if evidence_path.is_file() else {}
)
evidence = {
    name: dict(row)
    for name, row in loaded_evidence.items()
    if name in fingerprints and row.get("fingerprint") == fingerprints[name]
}
loaded_attempts = (
    json.loads(attempts_path.read_text(encoding="utf-8")) if attempts_path.is_file() else {}
)
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
        grep_results = sdk.content.grep(pattern, sources=chunk, context_lines=2)

        seen_matches = set()
        for result in grep_results:
            if result is None:
                continue
            for match in result.matches:
                if reads_for_constraint >= READ_LIMIT_PER_CONSTRAINT:
                    break
                if result.source in seen_matches:
                    continue
                seen_matches.add(result.source)
                reads_for_constraint += 1
                passage = sdk.content.read(
                    result.source,
                    start_line=max(match.line - 10, 1),
                    line_count=40,
                    max_chars=16_000,
                )
                if passage is None:
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

        if name in evidence or reads_for_constraint >= READ_LIMIT_PER_CONSTRAINT:
            break

if evidence:
    evidence_pending = evidence_path.with_suffix(".tmp")
    evidence_pending.write_text(json.dumps(evidence, ensure_ascii=False), encoding="utf-8")
    evidence_pending.replace(evidence_path)
attempts_pending = attempts_path.with_suffix(".tmp")
attempts_pending.write_text(
    json.dumps(
        {
            name: {"fingerprint": fingerprints[name], "sources": sorted(sources)}
            for name, sources in attempted.items()
        }
    ),
    encoding="utf-8",
)
attempts_pending.replace(attempts_path)

missing = [name for name in constraints if name not in evidence]
print("unsupported:", missing or "none")

if evidence and not missing:
    for name, row in evidence.items():
        excerpt = " ".join(row["text"].split())[:500]
        title = pool_by_source.get(row["source"], {}).get("title")
        print(f"EVIDENCE {name}: source={row['source']!r} title={title!r} text={excerpt!r}")
