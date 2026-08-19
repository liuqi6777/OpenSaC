# Search-as-Code patterns

Use this reference when a weaker model needs a small program to adapt. Pick one stage; do not
concatenate every example into one call. These examples intentionally avoid the workspace. Use
them when the useful result fits one observation; load the stateful reference when later programs
must recover a growing candidate or evidence ledger.

## Choose the stage

- **Explore** when the next query, ref, or matching rule depends on understanding search results.
  Show a bounded shortlist and stop for model judgment.
- **Rank passages** when a fused shortlist is available but the relevant document sections are
  not. Search, fuse, and passage ranking can stay in one deterministic stage.
- **Verify** when refs and checks are already concrete. Let Python grep, read, validate, and submit
  without an unnecessary model round trip.

The test is simple: if Python can choose the next input by an explicit rule, keep going; if the
choice requires interpreting language, end the stage with `NEXT:`.

## Explore candidates

This stage intentionally stops after search. It prints no raw result objects and at most eight
candidates, but includes each opaque ref so the next stage can reuse it exactly.

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
    batches = sdk.search.many(queries, limit_per_query=10, concurrency=4)
except BrokerError as error:
    print(f"ERROR: search code={error.code} retryable={error.retryable}")
else:
    candidates = sdk.search.fuse_rrf(batches, k=60)[:8]
    for item in candidates:
        snippet = " ".join((item.snippet or "").split())[:240]
        print(
            f"CANDIDATE ref={item.ref!r} date={item.date or '-'} "
            f"domain={item.domain or '-'} title={item.title or '(untitled)'} "
            f"snippet={snippet!r}"
        )
    failed = sum(batch.failure is not None for batch in batches)
    if candidates:
        print(
            f"NEXT: inspect {len(candidates)} candidates and choose refs/checks; "
            f"failed_queries={failed}"
        )
    else:
        print(f"NEXT: rewrite or broaden the queries; failed_queries={failed}")
```

## Rank passages across fused candidates

This is the default semantic evidence funnel. Passage text is exact document text, but the score
only orders this one report; inspect the text and source before trusting or citing it.

```python
from opensac_sdk import BrokerError, sdk

goal = "replace with the evidence question"
queries = [
    "entity relation exact terms",
    "entity relation alternate wording",
    "rare clue likely primary source",
]

try:
    batches = sdk.search.many(queries, limit_per_query=10, concurrency=4)
    fused = sdk.search.fuse_rrf(batches, k=60, limit=12)
    report = sdk.content.passages(
        goal,
        [item.ref for item in fused],
        limit=8,
        max_per_ref=2,
    )
except BrokerError as error:
    print(f"ERROR: evidence retrieval code={error.code} retryable={error.retryable}")
else:
    for item in report.passages:
        excerpt = " ".join(item.text.split())[:700]
        locator = dict(item.locator) if item.locator else None
        print(
            f"PASSAGE rank={item.rank} ref={item.ref!r} title={item.title!r} "
            f"coordinates={dict(item.coordinates)!r} "
            f"locator={locator!r} text={excerpt!r}"
        )
    failures = [item.failure.code for item in report.failures]
    print(
        "NEXT: inspect source quality and passage entailment; use grep/read for exact "
        f"checks or context, then submit verified locators; failures={failures[:4]}"
    )
```

## Verify selected refs and submit

Use exact refs chosen from exploration. Grep and read stay together because their next inputs are
mechanical. The program submits when all checks pass; otherwise it returns bounded evidence and a
`NEXT:` decision for the control model.

```python
import re

from opensac_sdk import BrokerError, sdk

refs = ["copy-ref-1-exactly", "copy-ref-2-exactly"]
checks = {
    "phrase": r"(target phrase|other spelling)",
    "year": r"\b(1998|1999)\b",
}

evidence = {}
problems = []
for name, pattern in checks.items():
    try:
        report = sdk.content.grep_report(refs, pattern, context=2)
    except BrokerError as error:
        problems.append(f"{name}:grep:{error.code}")
        continue
    problems.extend(f"{name}:fetch:{item.failure.code}" for item in report.failures)

    seen = set()
    for match in report.matches:
        if match.ref in seen:
            continue
        if len(seen) >= 4:
            break
        seen.add(match.ref)
        try:
            passage = sdk.content.read(
                [match.ref], offset=max(match.line - 10, 1), limit=40, max_chars=16_000
            )[0]
        except BrokerError as error:
            problems.append(f"{name}:read:{error.code}")
            continue
        if passage.failure is not None or not passage.text.strip() or passage.locator is None:
            problems.append(f"{name}:unreadable")
            continue
        if re.search(pattern, passage.text, re.IGNORECASE) is None:
            continue
        evidence[name] = {
            "ref": passage.ref,
            "text": passage.text,
            "locator": dict(passage.locator),
        }
        break

missing = sorted(set(checks) - evidence.keys())
if missing:
    for name, row in evidence.items():
        excerpt = " ".join(row["text"].split())[:500]
        print(f"EVIDENCE {name}: ref={row['ref']!r} text={excerpt!r}")
    print(f"NEXT: revise refs/checks for missing={missing}; problems={problems[:4]}")
else:
    sdk.output.submit(
        {
            "evidence": [
                {"constraint": name, "ref": row["ref"], "text": row["text"][:2_000]}
                for name, row in evidence.items()
            ]
        },
        citations=[{"ref": row["ref"], "locator": row["locator"]} for row in evidence.values()],
    )
```

Use a relation-specific check. If text presence alone cannot verify the requested relationship,
load the Python recipes routed from `SKILL.md` instead of treating a keyword match as proof.
