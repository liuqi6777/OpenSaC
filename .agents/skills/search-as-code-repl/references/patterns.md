# Search-as-Code patterns

These programs are independent, adaptable examples, not a required pipeline. Combine, split, skip,
or reorder them when the task calls for a different strategy. Only the final optional pattern uses
workspace artifacts, for a result that a later program will reuse.

## Example building blocks

- **Explore candidates** demonstrates bounded multi-query search and reusable source output.
- **Compose retrieval and inspection** demonstrates stateless search, ranking, full-text
  materialization, and optional structured views.
- **Verify and return evidence** demonstrates exact checks, context expansion, and the optional
  structured-output branch.
- **Extract structured fields** demonstrates aligned transformation of already inspected evidence.
- **Cache selected fetches across calls** demonstrates recovery ordering without prescribing how
  sources were selected or how cached text will be inspected.

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

Use this composed form only when metadata makes source selection mechanical. It selects a small
subset rather than treating the fused pool as a fetch queue. If choosing sources requires semantic
judgment, stop after **Explore candidates** and put only the chosen source strings in the next
program. This example fetches each selected source once and performs all evidence localization in
ordinary Python. Adapt the selection to the task.

```python
import re

from opensac_sdk import BrokerError, sdk

queries = [
    "entity relation exact terms",
    "entity relation alternate wording",
    "rare clue likely primary source",
]
local_patterns = [r"exact phrase", r"alternate spelling"]

try:
    search_outcomes = sdk.search.many(queries, limit=10, concurrency=4)
    fused = sdk.search.fuse_rrf(search_outcomes, k=60, limit=12)

    selected = []
    seen_sources = set()
    for candidate in fused:
        if candidate.source in seen_sources:
            continue
        selected.append(candidate)
        seen_sources.add(candidate.source)
        if len(selected) >= 4:
            break

    documents = {}
    fetch_failures = []
    fetch_outcomes = sdk.content.fetch_many(
        [candidate.source for candidate in selected],
        concurrency=4,
    )
    for outcome in fetch_outcomes:
        if outcome.status != "success" or outcome.document is None:
            code = outcome.error.code if outcome.error is not None else "invalid_outcome"
            fetch_failures.append(f"{outcome.source}:{code}")
            continue
        document = outcome.document
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
except BrokerError as error:
    print(f"ERROR: evidence retrieval code={error.code} retryable={error.retryable}")
else:
    for item in local_evidence[:4]:
        excerpt = " ".join(item["text"].split())[:500]
        print(f"LOCAL_EVIDENCE source={item['source']!r} text={excerpt!r}")
    failures = [
        outcome.error.code if outcome.error is not None else "unknown"
        for outcome in search_outcomes
        if outcome.status != "success"
    ]
    failures.extend(fetch_failures)
    print(
        "NEXT: judge coverage; select another small relevant batch only for unresolved constraints; "
        f"selected={len(selected)} failures={failures[:4]}"
    )
```

## Verify selected sources and return evidence

Use a small exact source set chosen from exploration. Fetch is always the first content operation for
each source: this example fetches each selected source once and runs all checks locally. Do not spend
additional content operations merely to rediscover or reformat text already returned by fetch.
Persist one full-text copy only when a later program will reuse it. Complete text stays local. The
program prints bounded, source-scoped runtime evidence for the calling agent.

```python
import re

from opensac_sdk import BrokerError, sdk

sources = ["selected-source-url-1", "selected-source-url-2"]
checks = {
    "phrase": r"(target phrase|other spelling)",
    "year": r"\b(1998|1999)\b",
}

evidence = {}
problems = []
documents = []
try:
    fetch_outcomes = sdk.content.fetch_many(sources, concurrency=2)
except BrokerError as error:
    problems.append(f"fetch_many:{error.code}")
else:
    for outcome in fetch_outcomes:
        if outcome.status != "success" or outcome.document is None:
            code = outcome.error.code if outcome.error is not None else "invalid_outcome"
            problems.append(f"{outcome.source}:fetch:{code}")
            continue
        document = outcome.document
        if not document.text.strip():
            problems.append(f"{outcome.source}:unreadable")
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
    for name, row in evidence.items():
        print(
            f"EVIDENCE {name}: source={row['source']!r} "
            f"coordinates={row['coordinates']!r} text={row['text']!r}"
        )
    print("READY: synthesize the user-facing answer from this verified evidence")
```

Use a relation-specific check. If text presence alone cannot verify the requested relationship,
adapt the check or use the optional structured extraction pattern below. Do not treat unrelated
keyword matches as proof.

## Optionally extract structured fields from inspected evidence

Use extraction only to transform bounded text that has already been inspected. Keep each input next
to its aligned outcome because `extract_many` does not return the original item. A schema-valid
result remains unsupported until its quote is found verbatim in that input.

```python
from opensac_sdk import BrokerError, sdk

evidence_items = [
    {
        "source": "selected-source-url-1",
        "text": "A bounded excerpt that states the relation being checked.",
    }
]
schema = {
    "type": "object",
    "properties": {
        "claim": {"type": "string"},
        "quote": {"type": "string"},
    },
    "required": ["claim", "quote"],
    "additionalProperties": False,
}

try:
    outcomes = sdk.llm.extract_many(
        evidence_items,
        instruction="Extract the stated relation and an exact supporting quote.",
        schema=schema,
    )
except BrokerError as error:
    print(f"ERROR: extraction code={error.code} retryable={error.retryable}")
else:
    verified = []
    failures = []
    for item, outcome in zip(evidence_items, outcomes, strict=True):
        if outcome.status != "success" or outcome.data is None:
            code = outcome.error.code if outcome.error is not None else "invalid_outcome"
            failures.append({"source": item["source"], "code": code})
            continue
        quote = outcome.data["quote"]
        if not quote or quote not in item["text"]:
            failures.append({"source": item["source"], "code": "quote_not_in_input"})
            continue
        verified.append(
            {"source": item["source"], "claim": outcome.data["claim"], "quote": quote}
        )

    for row in verified:
        print(f"EXTRACTED source={row['source']!r} claim={row['claim']!r} quote={row['quote']!r}")
    if failures:
        print(f"EXTRACTION_FAILURES {failures!r}")
```

## Optionally cache selected fetches across calls

Use this pattern only when a later program will reuse fetched text. The selected sources are inputs
to the pattern, not a search or stopping policy. One cumulative row stores the requested source,
terminal result, and returned canonical source without a separate workflow ledger.

```python
from opensac_sdk import BrokerError, sdk

selected_sources = ["selected-source-url-1", "selected-source-url-2"]
cache_path = "fetch-cache.jsonl"


def cache_row(requested_source, status, *, document=None, error=None):
    return {
        "requested_source": requested_source,
        "status": status,
        "source": document.source if document is not None else "",
        "text": document.text if document is not None else "",
        "error": error or {},
    }


cached_rows = (
    sdk.workspace.read_jsonl(cache_path) if sdk.workspace.exists(cache_path) else []
)
cached = {row["requested_source"]: dict(row) for row in cached_rows}
pending = [source for source in selected_sources if source not in cached]

if pending:
    sdk.workspace.upsert_jsonl(
        cache_path,
        [cache_row(source, "started") for source in pending],
        key="requested_source",
    )

    try:
        outcomes = sdk.content.fetch_many(pending)
    except BrokerError as error:
        terminal_rows = [
            cache_row(
                source,
                "failure",
                error={"code": error.code, "retryable": error.retryable},
            )
            for source in pending
        ]
    else:
        terminal_rows = []
        for outcome in outcomes:
            if outcome.status == "success" and outcome.document is not None:
                terminal_rows.append(
                    cache_row(outcome.source, "success", document=outcome.document)
                )
            else:
                failure = (
                    dict(outcome.error)
                    if outcome.error is not None
                    else {"code": "invalid_outcome"}
                )
                terminal_rows.append(
                    cache_row(outcome.source, "failure", error=failure)
                )

    # Make every external-call outcome durable before local parsing or other transformations.
    sdk.workspace.upsert_jsonl(cache_path, terminal_rows, key="requested_source")
    cached.update({row["requested_source"]: row for row in terminal_rows})

for requested_source in selected_sources:
    row = cached[requested_source]
    error_code = row["error"].get("code", "-")
    print(
        f"CACHE status={row['status']} requested={requested_source!r} "
        f"source={row['source']!r} error={error_code}"
    )
```

The example stores full text because cross-program reuse is its premise; store only the bounded data
the later program needs when full text is unnecessary. `requested_source` prevents unchanged replay,
while `source` records the canonical value returned by fetch. A surviving `started` row has an
unknown outcome. Retry only when durable workspace data proves the operation is missing.
