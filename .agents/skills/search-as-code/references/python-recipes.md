# Python orchestration recipes

Use these fragments when deterministic Python can replace control-model turns. Pick only the
recipes needed by the current stage, keep every generated collection bounded, and preserve the
stage-ending `NEXT:` or `submit` protocol from `SKILL.md`.

## Contents

- [Build bounded query grids](#build-bounded-query-grids)
- [Filter and aggregate candidates](#filter-and-aggregate-candidates)
- [Validate structured extraction](#validate-structured-extraction)
- [Turn extraction into one bounded action](#turn-extraction-into-one-bounded-action)

## Build bounded query grids

Use a comprehension when query variants follow an explicit template. Keep dimensions small and
cap the final list before calling search.

```python
entities = ["target organization", "former organization name"]
years = [str(year) for year in range(2019, 2024)]
clues = ["annual report", "leadership change"]
MAX_QUERIES = 24

queries = [f'"{entity}" {year} {clue}' for entity in entities for year in years for clue in clues]
queries = list(dict.fromkeys(queries))[:MAX_QUERIES]
```

Prefer one query per year when backend query syntax is unknown. Use `itertools.product` only when
the combination tuples themselves will be reused; a comprehension is usually easier to adapt.

## Filter and aggregate candidates

Filtering search metadata is triage, not evidence. Use a named predicate when the rule has several
parts or will be reused; otherwise prefer a comprehension. Decide whether an empty filtered set
means “broaden the heuristic” or “the hard constraint is unsupported.”

```python
from opensac_sdk import sdk

search_report = sdk.search.many(queries, limit=12, concurrency=4)
candidates = sdk.search.fuse_rrf(
    search_report,
    k=60,
    exclude_domains=["example.social"],
    domain_weights={"example.gov": 1.5, "example.edu": 1.25},
    max_per_domain=3,
)
wanted_years = {str(year) for year in range(2019, 2024)}
preferred_domains = {"example.gov", "example.edu"}


def searchable_text(item):
    return " ".join((item.title, item.snippet, item.date or "")).lower()


def keep(item):
    text = searchable_text(item)
    domain = (item.domain or "").lower()
    year_ok = any(year in text for year in wanted_years)
    domain_ok = not preferred_domains or any(
        domain == allowed or domain.endswith(f".{allowed}") for allowed in preferred_domains
    )
    return year_ok and domain_ok


shortlist = list(filter(keep, candidates))[:12]
multi_query_hits = [item for item in shortlist if len(item.provenance) >= 2]
coverage = {
    year: [item.source for item in shortlist if year in searchable_text(item)]
    for year in wanted_years
}
missing_years = sorted(year for year, sources in coverage.items() if not sources)
```

`len(item.provenance) >= 2` means multiple queries retrieved the candidate; it does not mean two
independent sources corroborate a claim.

## Validate structured extraction

`extract` transforms one supplied item. It cannot call search or content tools, and it must never
create sources. Loop in Python, keep the input beside each result, and catch `BrokerError` per item.
Ask for semantic fields and a quote; let Python validate the quote against the original passage.

```python
from opensac_sdk import BrokerError, sdk

usable = [passage for passage in passages if passage.text.strip()][:12]
items = [{"source": passage.source, "text": passage.text} for passage in usable]
schema = {
    "type": "object",
    "properties": {
        "next_action": {"type": "string", "enum": ["accept", "search_more", "reject"]},
        "evidence_quote": {"type": ["string", "null"]},
        "followup_queries": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 2,
        },
    },
    "required": ["next_action", "evidence_quote", "followup_queries"],
    "additionalProperties": False,
}

accepted = []
suggested_queries = []
extraction_failures = []
for passage, item in zip(usable, items, strict=True):
    try:
        data = sdk.llm.extract(
            item,
            instruction=(
                "Accept only when the passage explicitly states the requested relation. "
                "Otherwise reject it or propose at most two focused search queries."
            ),
            schema=schema,
            repair_attempts=1,
        )
    except BrokerError as error:
        extraction_failures.append(f"{error.code}:{error.retryable}")
    else:
        quote = data.get("evidence_quote")
        if data.get("next_action") == "accept" and quote and quote in item["text"]:
            accepted.append(
                {
                    "source": passage.source,
                    "text": passage.text,
                    "quote": quote,
                }
            )
        elif data.get("next_action") == "search_more":
            suggested_queries.extend(data.get("followup_queries", []))
```

If the pipeline model is unavailable or inconclusive, return to deterministic checks or end with
`NEXT:`. A schema-valid object is still unsupported until its quote passes the membership check.

## Turn extraction into one bounded action

Python—not the extraction model—decides whether to submit or make one follow-up search. Clean,
deduplicate, and cap model-proposed strings. Do not build an unbounded semantic-action loop.

```python
MAX_FOLLOWUPS = 6
followup_queries = list(
    dict.fromkeys(
        " ".join(query.split())[:200]
        for query in suggested_queries
        if isinstance(query, str) and query.strip()
    )
)[:MAX_FOLLOWUPS]

if accepted:
    sdk.output.submit(
        {
            "evidence": [
                {"source": row["source"], "text": row["text"][:2_000], "quote": row["quote"]}
                for row in accepted
            ]
        },
        citations=list(dict.fromkeys(row["source"] for row in accepted)),
    )
elif followup_queries:
    try:
        search_report = sdk.search.many(followup_queries, limit=8, concurrency=4)
    except BrokerError as error:
        print(f"ERROR: follow-up search code={error.code} retryable={error.retryable}")
        print("NEXT: change the source or query strategy")
    else:
        candidates = sdk.search.fuse_rrf(search_report, k=60)[:8]
        for item in candidates:
            print(f"CANDIDATE source={item.source!r} title={item.title!r}")
        print("NEXT: inspect follow-up candidates and choose sources/checks")
else:
    print(f"NEXT: use deterministic checks; extraction_failures={extraction_failures[:4]!r}")
```
