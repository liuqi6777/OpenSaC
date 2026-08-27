# Persistent Search-as-Code patterns

These cells are adaptable examples, not a required research pipeline. Compose mechanically dependent
work in one cell, and split when the next input needs semantic judgment or when live-state inspection
is useful. Values left by a successful cell remain available while the interpreter stays ready.

## Explore candidates when judgment is needed

Use a search-only checkpoint when titles and snippets must be understood before choosing the next
sources or checks. The bounded pool remains live; no serialization is needed.

```python
from opensac_sdk import BrokerError, sdk

research_goal = "replace with the evidence question"
queries = [
    '"exact phrase" narrowing words',
    "entity relation alternate wording",
    "rare clue likely primary source",
]

try:
    search_report = sdk.search.many(queries, limit_per_query=10, concurrency=4)
except BrokerError as error:
    print(f"ERROR: search code={error.code} retryable={error.retryable}")
else:
    candidate_pool = sdk.search.fuse_rrf(search_report, k=60, limit=16)
    for item in candidate_pool[:8]:
        snippet = " ".join((item.snippet or "").split())[:240]
        print(
            f"CANDIDATE source={item.source!r} domain={item.domain or '-'} "
            f"title={item.title or '(untitled)'} snippet={snippet!r}"
        )
    print(
        "NEXT: choose sources and checks; "
        "reuse research_goal, candidate_pool, search_report; "
        f"failed_queries={len(search_report.failures)}"
    )
```

## Compose retrieval and focused inspection

This is the default semantic evidence funnel. Search, fusion, passage selection, and focused reads
are mechanically connected, so the cell keeps them together and leaves normalized evidence windows
live for later semantic checks.

```python
from opensac_sdk import BrokerError, sdk

research_goal = "replace with the evidence question"
queries = [
    "entity relation exact terms",
    "entity relation alternate wording",
    "rare clue likely primary source",
]

try:
    search_report = sdk.search.many(queries, limit_per_query=10, concurrency=4)
    candidate_pool = sdk.search.fuse_rrf(search_report, k=60, limit=12)
    passage_report = sdk.content.passages(
        research_goal,
        [item.source for item in candidate_pool],
        limit=8,
        max_per_source=2,
    )
    read_windows = []
    seen_windows = set()
    for passage in passage_report.passages:
        key = (passage.source, passage.coordinates["start_line"])
        if key in seen_windows:
            continue
        seen_windows.add(key)
        read_windows.append(
            {
                "source": passage.source,
                "offset": max(passage.coordinates["start_line"] - 8, 1),
                "limit": 50,
                "max_chars": 16_000,
                "passage_coordinates": dict(passage.coordinates),
            }
        )
        if len(read_windows) >= 6:
            break
    read_report = sdk.content.read_many(
        [
            {key: row[key] for key in ("source", "offset", "limit", "max_chars")}
            for row in read_windows
        ]
    ) if read_windows else None
except BrokerError as error:
    print(f"ERROR: evidence retrieval code={error.code} retryable={error.retryable}")
else:
    window_by_index = {index: row for index, row in enumerate(read_windows)}
    evidence_windows = []
    for item in (read_report.results if read_report else []):
        window = window_by_index[item.input_index]
        evidence_windows.append(
            {
                "source": item.source,
                "title": item.title,
                "text": item.text,
                "coordinates": {
                    "passage": window["passage_coordinates"],
                    "start_line": item.metadata.get("start_line"),
                    "end_line": item.metadata.get("end_line"),
                },
            }
        )
    for row in evidence_windows[:4]:
        excerpt = " ".join(row["text"].split())[:600]
        print(
            f"EVIDENCE source={row['source']!r} title={row['title']!r} "
            f"coordinates={row['coordinates']!r} text={excerpt!r}"
        )
    retrieval_failures = [item.code for item in search_report.failures]
    retrieval_failures.extend(item.code for item in passage_report.failures)
    if read_report:
        retrieval_failures.extend(item.code for item in read_report.failures)
    print(
        "NEXT: judge source quality and entailment; reuse evidence_windows and candidate_pool; "
        f"failures={retrieval_failures[:4]}"
    )
```

## Verify selected sources and return evidence

Adapt `checks` after inspecting the prior observation. Successful checks remain live so a later cell
can refine only unresolved requirements. Structured output is optional.

```python
import re

from opensac_sdk import BrokerError, sdk

selected_sources = (
    list(dict.fromkeys(row["source"] for row in evidence_windows))[:6]
    if "evidence_windows" in globals()
    else ["selected-source-url-1", "selected-source-url-2"]
)
structured_output_requested = False
checks = {
    "phrase": r"(target phrase|other spelling)",
    "year": r"\b(1998|1999)\b",
}
if "verified_evidence" not in globals():
    verified_evidence = {}
verification_problems = []

for name, pattern in checks.items():
    if name in verified_evidence:
        continue
    try:
        report = sdk.content.grep(selected_sources, pattern, context=2)
    except BrokerError as error:
        verification_problems.append(f"{name}:grep:{error.code}")
        continue
    verification_problems.extend(f"{name}:fetch:{item.code}" for item in report.failures)
    seen_sources = set()
    for match in report.matches:
        if match.source in seen_sources:
            continue
        if len(seen_sources) >= 4:
            break
        seen_sources.add(match.source)
        try:
            passage = sdk.content.read(
                match.source,
                offset=max(match.line - 10, 1),
                limit=40,
                max_chars=16_000,
            )
        except BrokerError as error:
            verification_problems.append(f"{name}:read:{error.code}")
            continue
        if re.search(pattern, passage.text, re.IGNORECASE):
            verified_evidence[name] = {
                "source": passage.source,
                "text": passage.text,
            }
            break

missing_checks = sorted(set(checks) - verified_evidence)
if missing_checks:
    for name, row in verified_evidence.items():
        excerpt = " ".join(row["text"].split())[:500]
        print(f"EVIDENCE {name}: source={row['source']!r} text={excerpt!r}")
    print(
        f"NEXT: refine missing checks {missing_checks}; reuse verified_evidence; "
        f"problems={verification_problems[:4]}"
    )
else:
    result = {
        "evidence": [
            {"constraint": name, "source": row["source"], "text": row["text"][:2_000]}
            for name, row in verified_evidence.items()
        ]
    }
    if structured_output_requested:
        sdk.output.submit(
            result,
            citations=list(
                dict.fromkeys(row["source"] for row in verified_evidence.values())
            ),
        )
    else:
        for item in result["evidence"]:
            excerpt = " ".join(item["text"].split())[:500]
            print(f"EVIDENCE {item['constraint']}: source={item['source']!r} text={excerpt!r}")
        print("NEXT: synthesize the user-facing answer from this verified evidence")
```

Overwrite or delete superseded values when namespace size or ambiguity makes cleanup useful. A
relation-specific claim still needs a relation-specific check; keyword presence alone is not proof.
