# Search-as-Code patterns

These programs are independent, adaptable examples, not a required pipeline. Combine, split, skip,
or reorder them when the task calls for a different strategy. They intentionally avoid the
workspace; use the stateful reference only when durable artifacts are useful across calls.

## Example building blocks

- **Explore candidates** demonstrates bounded multi-query search and reusable source output.
- **Compose retrieval and inspection** demonstrates stateless search, ranking, full-text
  materialization, and optional structured views.
- **Verify and return evidence** demonstrates exact checks, context expansion, and the optional
  structured-output branch.

Each block illustrates capability mechanics. Its query count, bounds, call grouping, and stopping
point are examples for the agent to adapt.

## Explore candidates

This stage intentionally stops after search. It prints no raw result objects and at most eight
candidates, including each URL or local document ID so the next stage can reuse it exactly.

```python
from opensac_sdk import BrokerError, sdk

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
    outcomes = sdk.search.many(queries, limit=10, concurrency=4)
except BrokerError as error:
    print(f"ERROR: search code={error.code} retryable={error.retryable}")
else:
    candidates = sdk.search.fuse_rrf(outcomes, k=60)[:8]
    for item in candidates:
        snippet = " ".join((item.snippet or "").split())[:240]
        print(
            f"CANDIDATE source={item.source!r} date={item.date or '-'} "
            f"domain={item.domain or '-'} title={item.title or '(untitled)'} "
            f"snippet={snippet!r}"
        )
    failed = sum(outcome.status != "success" for outcome in outcomes)
    if candidates:
        print(
            f"NEXT: choose a small relevant subset from {len(candidates)} candidates; "
            f"failed_queries={failed}"
        )
    else:
        print(f"NEXT: rewrite or broaden the queries; failed_queries={failed}")
```

## Compose retrieval and focused inspection

Use this composed form only when metadata makes source selection mechanical. It keeps a small,
source-diverse fetch batch rather than treating the fused pool as a fetch queue. If choosing sources
requires semantic judgment, stop after **Explore candidates** and put only the chosen source strings
in the next program. This example combines local exact checks with optional semantic passage ranking;
either branch can be omitted. Adapt the selection signals and bounds to the task.

```python
import re

from opensac_sdk import BrokerError, sdk

goal = "replace with the evidence question"
queries = [
    "entity relation exact terms",
    "entity relation alternate wording",
    "rare clue likely primary source",
]
local_patterns = [r"exact phrase", r"alternate spelling"]
fetch_batch = 4

try:
    search_outcomes = sdk.search.many(queries, limit=10, concurrency=4)
    fused = sdk.search.fuse_rrf(search_outcomes, k=60, limit=12)

    selected = []
    seen_families = set()
    for candidate in fused:
        family = candidate.domain or candidate.source
        if family in seen_families:
            continue
        selected.append(candidate)
        seen_families.add(family)
        if len(selected) >= fetch_batch:
            break

    documents = {}
    fetch_failures = []
    for candidate in selected:
        try:
            document = sdk.content.fetch(candidate.source)
        except BrokerError as error:
            fetch_failures.append(f"{candidate.source}:{error.code}")
        else:
            documents[document.source] = document

    local_evidence = []
    compiled = [re.compile(pattern, re.IGNORECASE) for pattern in local_patterns]
    for document in documents.values():
        for pattern in compiled:
            match = pattern.search(document.text)
            if match is None:
                continue
            start = max(0, match.start() - 200)
            end = min(len(document.text), match.end() + 300, start + 700)
            local_evidence.append({"source": document.source, "text": document.text[start:end]})
            break

    # Keep this branch when semantic localization adds value; otherwise local_evidence is enough.
    passage_report = (
        sdk.content.passages(goal, sources=list(documents), limit=6, limit_per_source=2)
        if documents
        else None
    )
except BrokerError as error:
    print(f"ERROR: evidence retrieval code={error.code} retryable={error.retryable}")
else:
    for item in local_evidence[:2]:
        excerpt = " ".join(item["text"].split())[:500]
        print(f"LOCAL_EVIDENCE source={item['source']!r} text={excerpt!r}")
    if passage_report is not None:
        for passage in passage_report.passages[:4]:
            excerpt = " ".join(passage.text.split())[:500]
            print(
                f"SEMANTIC_EVIDENCE source={passage.source!r} "
                f"coordinates={dict(passage.coordinates)!r} text={excerpt!r}"
            )
    failures = [
        outcome.error.code if outcome.error is not None else "unknown"
        for outcome in search_outcomes
        if outcome.status != "success"
    ]
    failures.extend(fetch_failures)
    if passage_report is not None:
        failures.extend(item.code for item in passage_report.failures)
    print(
        "NEXT: judge coverage; select another small relevant batch only for unresolved constraints; "
        f"selected={len(selected)} failures={failures[:4]}"
    )
```

## Verify selected sources and return evidence

Use a small exact source set chosen from exploration. Fetch is always the first content operation for
each source: this example fetches each selected source once and runs all checks locally. A later
`passages`, `grep`, or `read` call may reuse the session cache but still consumes logical
content-fetch budget. Use `passages` when semantic ranking adds value; local Python usually covers
ordinary `grep`/`read` work.
Persist one full-text copy only when a later program will reuse it. Complete text stays local. The
program returns bounded runtime evidence by default and submits it only when the caller or downstream
contract needs `ExecResult.output`.

```python
import re

from opensac_sdk import BrokerError, sdk

sources = ["selected-source-url-1", "selected-source-url-2"]
structured_output_requested = False
checks = {
    "phrase": r"(target phrase|other spelling)",
    "year": r"\b(1998|1999)\b",
}

evidence = {}
problems = []
documents = []
for source in sources:
    try:
        document = sdk.content.fetch(source)
    except BrokerError as error:
        problems.append(f"{source}:fetch:{error.code}")
        continue
    if not document.text.strip():
        problems.append(f"{source}:unreadable")
        continue
    documents.append(document)

for name, pattern in checks.items():
    compiled = re.compile(pattern, re.IGNORECASE)
    for document in documents:
        match = compiled.search(document.text)
        if match is None:
            continue
        excerpt_start = max(0, match.start() - 200)
        excerpt_end = min(len(document.text), match.end() + 300, excerpt_start + 700)
        evidence[name] = {
            "source": document.source,
            "text": document.text[excerpt_start:excerpt_end],
            "coordinates": {
                "match_start_line": document.text.count("\n", 0, match.start()) + 1,
                "match_start_character": match.start(),
                "match_end_character": match.end(),
                "excerpt_start_character": excerpt_start,
                "excerpt_end_character": excerpt_end,
            },
        }
        break

missing = sorted(set(checks) - evidence.keys())
if missing:
    for name, row in evidence.items():
        print(
            f"EVIDENCE {name}: source={row['source']!r} "
            f"coordinates={row['coordinates']!r} text={row['text']!r}"
        )
    print(f"NEXT: revise sources/checks for missing={missing}; problems={problems[:4]}")
else:
    result = {
        "evidence": [
            {
                "constraint": name,
                "source": row["source"],
                "text": row["text"],
                "coordinates": row["coordinates"],
            }
            for name, row in evidence.items()
        ]
    }
    if structured_output_requested:
        sdk.output.submit(
            result,
            citations=list(dict.fromkeys(row["source"] for row in evidence.values())),
        )
    else:
        for item in result["evidence"]:
            print(
                f"EVIDENCE {item['constraint']}: source={item['source']!r} "
                f"coordinates={item['coordinates']!r} text={item['text']!r}"
            )
        print("NEXT: synthesize the user-facing answer from this verified evidence")
```

Use a relation-specific check. If text presence alone cannot verify the requested relationship,
load the Python recipes routed from `SKILL.md` instead of treating a keyword match as proof.
