# Persistent Search-as-Code patterns

These cells show one possible use of a live Python namespace, not a required research pipeline.
Combine, split, reorder, or skip them when their data dependencies allow it. Variable names, bounds,
and `NEXT:` lines are examples for the agent to adapt; avoid repeating completed external calls.

## Explore candidates

This cell creates a bounded candidate pool. Search result records remain live in memory, so the
next cell can rank passages without serializing the pool.

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
    failed_queries = len(search_report.failures)
    print(
        "NEXT: inspect candidates and rank evidence; "
        "reuse research_goal, candidate_pool, search_report; "
        f"failed_queries={failed_queries}"
    )
```

## Rank passages across the live pool

Run this only after `candidate_pool` exists. It performs no new search and overwrites any stale
`passage_report` from an earlier strategy.

```python
from opensac_sdk import BrokerError, sdk

try:
    passage_report = sdk.content.passages(
        research_goal,
        [item.source for item in candidate_pool],
        limit=8,
        max_per_source=2,
    )
except BrokerError as error:
    print(f"ERROR: passages code={error.code} retryable={error.retryable}")
else:
    for item in passage_report.passages:
        excerpt = " ".join(item.text.split())[:700]
        print(
            f"PASSAGE rank={item.rank} source={item.source!r} title={item.title!r} text={excerpt!r}"
        )
    passage_sources = list(dict.fromkeys(item.source for item in passage_report.passages))
    print(
        "NEXT: choose exact checks and verify selected passages; "
        "reuse passage_report, passage_sources, candidate_pool"
    )
```

## Verify selected sources and submit

Adapt `checks` after inspecting the previous observation. This cell keeps `verified_evidence` live;
if one check fails, the next cell can refine only that check without repeating successful work.

```python
import re

from opensac_sdk import BrokerError, sdk

selected_sources = passage_sources[:6]
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
    for match in report.matches[:6]:
        passage = sdk.content.read(
            match.source, offset=max(match.line - 10, 1), limit=40, max_chars=16_000
        )
        if re.search(pattern, passage.text, re.IGNORECASE):
            verified_evidence[name] = {
                "source": passage.source,
                "text": passage.text,
            }
            break

missing_checks = sorted(set(checks) - verified_evidence)
if missing_checks:
    print(
        f"NEXT: refine missing checks {missing_checks}; "
        "reuse verified_evidence, checks, selected_sources; "
        f"problems={verification_problems[:4]}"
    )
else:
    sdk.output.submit(
        {
            "evidence": [
                {"constraint": name, "source": row["source"], "text": row["text"][:2_000]}
                for name, row in verified_evidence.items()
            ]
        },
        citations=list(dict.fromkeys(row["source"] for row in verified_evidence.values())),
    )
```

Overwrite or delete superseded values when namespace size or ambiguity makes cleanup useful. A
relation-specific claim still needs a relation-specific check; keyword presence alone is not proof.
