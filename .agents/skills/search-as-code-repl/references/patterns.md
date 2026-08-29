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
    search_outcomes = sdk.search.many(queries, limit=10, concurrency=4)
except BrokerError as error:
    print(f"ERROR: search code={error.code} retryable={error.retryable}")
else:
    candidate_pool = sdk.search.fuse_rrf(search_outcomes, k=60, limit=16)
    for item in candidate_pool[:8]:
        snippet = " ".join((item.snippet or "").split())[:240]
        print(
            f"CANDIDATE source={item.source!r} domain={item.domain or '-'} "
            f"title={item.title or '(untitled)'} snippet={snippet!r}"
        )
    print(
        "NEXT: choose sources and checks; "
        "reuse research_goal, candidate_pool, search_outcomes; "
        f"failed_queries={sum(row.status != 'success' for row in search_outcomes)}"
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
    search_outcomes = sdk.search.many(queries, limit=10, concurrency=4)
    candidate_pool = sdk.search.fuse_rrf(search_outcomes, k=60, limit=12)
    passage_report = sdk.content.passages(
        research_goal,
        sources=[item.source for item in candidate_pool],
        limit=8,
        limit_per_source=2,
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
                "start_line": max(passage.coordinates["start_line"] - 8, 1),
                "line_count": 50,
                "max_chars": 16_000,
                "passage_coordinates": dict(passage.coordinates),
            }
        )
        if len(read_windows) >= 6:
            break
    read_results = []
    read_failures = []
    for window in read_windows:
        try:
            item = sdk.content.read(
                window["source"],
                start_line=window["start_line"],
                line_count=window["line_count"],
                max_chars=window["max_chars"],
            )
        except BrokerError as error:
            read_failures.append(error.code)
        else:
            read_results.append((window, item))
except BrokerError as error:
    print(f"ERROR: evidence retrieval code={error.code} retryable={error.retryable}")
else:
    evidence_windows = []
    for window, item in read_results:
        evidence_windows.append(
            {
                "source": item.source,
                "title": item.title,
                "text": item.text,
                "coordinates": {
                    "passage": window["passage_coordinates"],
                    "start_line": item.window.start_line,
                    "end_line": item.window.end_line,
                },
            }
        )
    for row in evidence_windows[:4]:
        excerpt = " ".join(row["text"].split())[:600]
        print(
            f"EVIDENCE source={row['source']!r} title={row['title']!r} "
            f"coordinates={row['coordinates']!r} text={excerpt!r}"
        )
    retrieval_failures = [
        outcome.error.code if outcome.error is not None else "unknown"
        for outcome in search_outcomes
        if outcome.status != "success"
    ]
    retrieval_failures.extend(item.code for item in passage_report.failures)
    retrieval_failures.extend(read_failures)
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
        grep_outcomes = sdk.content.grep(pattern, sources=selected_sources, context_lines=2)
    except BrokerError as error:
        verification_problems.append(f"{name}:grep:{error.code}")
        continue
    verification_problems.extend(
        f"{name}:fetch:{outcome.status}" for outcome in grep_outcomes if outcome.status != "success"
    )
    seen_sources = set()
    for outcome in grep_outcomes:
        if outcome.status != "success":
            continue
        if len(seen_sources) >= 4:
            break
        if not outcome.matches or outcome.source in seen_sources:
            continue
        seen_sources.add(outcome.source)
        match = outcome.matches[0]
        try:
            passage = sdk.content.read(
                outcome.source,
                start_line=max(match.line - 10, 1),
                line_count=40,
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
            citations=list(dict.fromkeys(row["source"] for row in verified_evidence.values())),
        )
    else:
        for item in result["evidence"]:
            excerpt = " ".join(item["text"].split())[:500]
            print(f"EVIDENCE {item['constraint']}: source={item['source']!r} text={excerpt!r}")
        print("NEXT: synthesize the user-facing answer from this verified evidence")
```

Overwrite or delete superseded values when namespace size or ambiguity makes cleanup useful. A
relation-specific claim still needs a relation-specific check; keyword presence alone is not proof.
