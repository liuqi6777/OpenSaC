# Search-as-Code patterns

These programs are independent, adaptable examples, not a required pipeline. Combine, split, skip,
or reorder them when the task calls for a different strategy. They intentionally avoid the
workspace; use the stateful reference only when durable artifacts are useful across calls.

## Example building blocks

- **Explore candidates** demonstrates bounded multi-query search and reusable source output.
- **Compose retrieval and inspection** demonstrates a complete stateless search, ranking, and focused
  read pipeline.
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
    report = sdk.search.many(queries, limit_per_query=10, concurrency=4)
except BrokerError as error:
    print(f"ERROR: search code={error.code} retryable={error.retryable}")
else:
    candidates = sdk.search.fuse_rrf(report, k=60)[:8]
    for item in candidates:
        snippet = " ".join((item.snippet or "").split())[:240]
        print(
            f"CANDIDATE source={item.source!r} date={item.date or '-'} "
            f"domain={item.domain or '-'} title={item.title or '(untitled)'} "
            f"snippet={snippet!r}"
        )
    failed = len(report.failures)
    if candidates:
        print(
            f"NEXT: inspect {len(candidates)} candidates and choose sources/checks; "
            f"failed_queries={failed}"
        )
    else:
        print(f"NEXT: rewrite or broaden the queries; failed_queries={failed}")
```

## Compose retrieval and focused inspection

This is the default semantic evidence funnel. It keeps the mechanically dependent search, ranking,
passage selection, and focused reads in one program without workspace state. Passage scores only
order this report; inspect the returned text and source before trusting it.

```python
from opensac_sdk import BrokerError, sdk

goal = "replace with the evidence question"
queries = [
    "entity relation exact terms",
    "entity relation alternate wording",
    "rare clue likely primary source",
]

try:
    search_report = sdk.search.many(queries, limit_per_query=10, concurrency=4)
    fused = sdk.search.fuse_rrf(search_report, k=60, limit=12)
    report = sdk.content.passages(
        goal,
        [item.source for item in fused],
        limit=8,
        max_per_source=2,
    )
    windows = []
    seen = set()
    for passage in report.passages:
        key = (passage.source, passage.coordinates["start_line"])
        if key in seen:
            continue
        seen.add(key)
        windows.append(
            {
                "source": passage.source,
                "offset": max(passage.coordinates["start_line"] - 8, 1),
                "limit": 50,
                "max_chars": 16_000,
                "coordinates": dict(passage.coordinates),
            }
        )
        if len(windows) >= 6:
            break
    read_report = sdk.content.read_many(
        [
            {key: row[key] for key in ("source", "offset", "limit", "max_chars")}
            for row in windows
        ]
    ) if windows else None
except BrokerError as error:
    print(f"ERROR: evidence retrieval code={error.code} retryable={error.retryable}")
else:
    window_by_index = {index: row for index, row in enumerate(windows)}
    for item in (read_report.results if read_report else [])[:4]:
        window = window_by_index[item.input_index]
        excerpt = " ".join(item.text.split())[:600]
        print(
            f"EVIDENCE source={item.source!r} title={item.title!r} "
            f"coordinates={window['coordinates']!r} "
            f"text={excerpt!r}"
        )
    failures = [item.code for item in search_report.failures]
    failures.extend(item.code for item in report.failures)
    if read_report:
        failures.extend(item.code for item in read_report.failures)
    print(
        "NEXT: judge source quality and entailment; refine only the unresolved constraints; "
        f"failures={failures[:4]}"
    )
```

## Verify selected sources and return evidence

Use exact sources chosen from exploration. Grep and read stay together because their next inputs are
mechanical. By default the program returns bounded runtime evidence for the control model to
synthesize. Set the structured-output flag only when the caller or downstream contract needs
`ExecResult.output`.

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
for name, pattern in checks.items():
    try:
        report = sdk.content.grep(sources, pattern, context=2)
    except BrokerError as error:
        problems.append(f"{name}:grep:{error.code}")
        continue
    problems.extend(f"{name}:fetch:{item.code}" for item in report.failures)

    seen = set()
    for match in report.matches:
        if match.source in seen:
            continue
        if len(seen) >= 4:
            break
        seen.add(match.source)
        try:
            passage = sdk.content.read(
                match.source, offset=max(match.line - 10, 1), limit=40, max_chars=16_000
            )
        except BrokerError as error:
            problems.append(f"{name}:read:{error.code}")
            continue
        if not passage.text.strip():
            problems.append(f"{name}:unreadable")
            continue
        if re.search(pattern, passage.text, re.IGNORECASE) is None:
            continue
        evidence[name] = {
            "source": passage.source,
            "text": passage.text,
        }
        break

missing = sorted(set(checks) - evidence.keys())
if missing:
    for name, row in evidence.items():
        excerpt = " ".join(row["text"].split())[:500]
        print(f"EVIDENCE {name}: source={row['source']!r} text={excerpt!r}")
    print(f"NEXT: revise sources/checks for missing={missing}; problems={problems[:4]}")
else:
    result = {
        "evidence": [
            {"constraint": name, "source": row["source"], "text": row["text"][:2_000]}
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
            excerpt = " ".join(item["text"].split())[:500]
            print(f"EVIDENCE {item['constraint']}: source={item['source']!r} text={excerpt!r}")
        print("NEXT: synthesize the user-facing answer from this verified evidence")
```

Use a relation-specific check. If text presence alone cannot verify the requested relationship,
load the Python recipes routed from `SKILL.md` instead of treating a keyword match as proof.
