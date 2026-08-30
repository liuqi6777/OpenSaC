# Search-as-Code patterns

These programs are independent, adaptable examples, not a required pipeline. Combine, split, skip,
or reorder them when the task calls for a different strategy. Workspace examples persist artifacts
only when a later program will reuse them.

## Example building blocks

- **Explore candidates** demonstrates bounded multi-query search and reusable source output.
- **Compose retrieval and inspection** demonstrates stateless search, ranking, full-text
  materialization, and optional structured views.
- **Verify and return evidence** demonstrates exact checks, context expansion, and a coverage summary.
- **Emit one globally bounded observation** demonstrates a single stdout budget across fan-out rows.
- **Cache selected fetches across calls** demonstrates recovery ordering without prescribing how
  sources were selected or how cached text will be inspected.

Closed-set and one-to-many tasks have a smaller dedicated reference:
[repeated units and record sets](repeated-units.md).

Each block illustrates capability mechanics. Its query count, bounds, call grouping, and stopping
point are examples for the agent to adapt.

## Emit one globally bounded observation

Use one emitter when a checkpoint can produce more than a few candidate, unit, or evidence rows.
Collect and normalize rows during capability handling; do not print from those loops. The emitter
keeps the source on every shown row, preserves room for counts, and makes omission explicit. Adapt
the row fields and limit, but keep one budget over every code path. `key` names the material
requirement or unit; keep `source` separate instead of constructing `source::field` keys.

```python
def one_line(value):
    return " ".join(str(value or "").split())


def emit_observation(rows, *, max_chars=3_800):
    primary_by_key = {}
    secondary = []
    seen = set()
    for row in rows:
        normalized = {
            "key": one_line(row.get("key")),
            "status": one_line(row.get("status")) or "unknown",
            "source": one_line(row.get("source")),
            "excerpt": one_line(row.get("excerpt"))[:180],
        }
        identity = tuple(normalized.values())
        if identity not in seen:
            seen.add(identity)
            if normalized["key"] not in primary_by_key:
                primary_by_key[normalized["key"]] = normalized
            else:
                secondary.append(normalized)

    # Shrink primary excerpts until every material key fits, then spend residual budget on extras.
    primary = list(primary_by_key.values())
    unique = [*primary, *secondary]
    failures = sum(row["status"] == "failed" for row in unique)

    def render(row, excerpt_chars):
        return (
            f"ROW key={row['key']!r} status={row['status']} "
            f"source={row['source']!r} excerpt={row['excerpt'][:excerpt_chars]!r}"
        )

    excerpt_chars = 180
    while True:
        primary_lines = [render(row, excerpt_chars) for row in primary]
        footer = (
            f"COUNTS total={len(unique)} shown={len(primary_lines)} "
            f"omitted={len(unique) - len(primary_lines)} failures={failures}"
        )
        if len("\n".join([*primary_lines, footer])) <= max_chars or excerpt_chars == 0:
            break
        excerpt_chars = max(0, excerpt_chars - 20)

    shown_lines = []
    for row in unique:
        line = render(row, excerpt_chars)
        next_shown = len(shown_lines) + 1
        footer = (
            f"COUNTS total={len(unique)} shown={next_shown} "
            f"omitted={len(unique) - next_shown} failures={failures}"
        )
        if len("\n".join([*shown_lines, line, footer])) > max_chars:
            break
        shown_lines.append(line)

    footer = (
        f"COUNTS total={len(unique)} shown={len(shown_lines)} "
        f"omitted={len(unique) - len(shown_lines)} "
        f"failures={failures}"
    )
    print("\n".join([*shown_lines, footer]))
```

The helper places the first row for each material key before secondary excerpts and shrinks primary
excerpts before omitting a key. It is only a projection: derive coverage from the full in-memory or
persisted rows, not from the visible subset. Adapt or replace it when a simpler fixed summary fits.

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
    print(f"FAILURE operation=search code={error.code} retryable={error.retryable}")
else:
    candidates = sdk.search.fuse_rrf(outcomes, k=60)[:8]
    failed = sum(outcome.status != "success" for outcome in outcomes)
    summary = f"COUNTS candidates={len(candidates)} failed_queries={failed}"
    lines = []
    for item in candidates:
        title = " ".join((item.title or "(untitled)").split())[:120]
        snippet = " ".join((item.snippet or "").split())[:200]
        line = (
            f"CANDIDATE source={item.source!r} date={item.date or '-'} "
            f"domain={item.domain or '-'} title={title!r} snippet={snippet!r}"
        )
        if len("\n".join([*lines, line, summary])) > 3_800:
            break
        lines.append(line)
    print("\n".join([*lines, summary]))
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
    print(f"FAILURE operation=evidence_retrieval code={error.code} retryable={error.retryable}")
else:
    failures = [
        outcome.error.code if outcome.error is not None else "unknown"
        for outcome in search_outcomes
        if outcome.status != "success"
    ]
    failures.extend(fetch_failures)
    summary = (
        f"COUNTS selected={len(selected)} evidence={len(local_evidence)} "
        f"failures={len(failures)} codes={failures[:4]!r}"
    )
    lines = []
    for item in local_evidence:
        excerpt = " ".join(item["text"].split())[:500]
        line = f"LOCAL_EVIDENCE source={item['source']!r} text={excerpt!r}"
        if len("\n".join([*lines, line, summary])) > 3_800:
            break
        lines.append(line)
    print("\n".join([*lines, summary]))
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
summary = (
    f"COVERAGE supported={len(evidence)}/{len(checks)} "
    f"missing={missing!r} problems={problems[:4]!r}"
)
lines = []
for name, row in evidence.items():
    line = (
        f"EVIDENCE {name}: source={row['source']!r} "
        f"coordinates={row['coordinates']!r} text={row['text']!r}"
    )
    if len("\n".join([*lines, line, summary])) > 3_800:
        break
    lines.append(line)
print("\n".join([*lines, summary]))
```

Use a relation-specific check. If text presence alone cannot verify the requested relationship,
adapt the deterministic parser or leave the field unsupported. Do not treat unrelated keyword
matches as proof.

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


cached_rows = sdk.workspace.read_jsonl(cache_path) if sdk.workspace.exists(cache_path) else []
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
                terminal_rows.append(cache_row(outcome.source, "failure", error=failure))

    # Make every external-call outcome durable before local parsing or other transformations.
    sdk.workspace.upsert_jsonl(cache_path, terminal_rows, key="requested_source")
    cached.update({row["requested_source"]: row for row in terminal_rows})

lines = []
for requested_source in selected_sources:
    row = cached[requested_source]
    lines.append(
        f"CACHE status={row['status']} requested={requested_source!r} "
        f"source={row['source']!r} error={row['error'].get('code', '-')}"
    )
unresolved = sum(cached[source]["status"] != "success" for source in selected_sources)
summary = f"COUNTS cached={len(selected_sources)} unresolved_fetches={unresolved}"
print("\n".join([*lines, summary]))
```

The example stores full text because cross-program reuse is its premise; store only the bounded data
the later program needs when full text is unnecessary. `requested_source` prevents unchanged replay,
while `source` records the canonical value returned by fetch. A surviving `started` row has an
unknown outcome. Retry only when durable workspace data proves the operation is missing. This
skeleton stops where a task-specific semantic choice may be needed. If the relation checks are already
known, append their local parsing, validation, evidence-row update, and emitter to this program instead
of submitting the cache skeleton as its own checkpoint.
