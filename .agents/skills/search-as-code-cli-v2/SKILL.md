---
name: search-as-code-cli-v2
description: Example-driven web research with OpenSAC Python through the local agent-run CLI. Use in shell-capable environments for web search, document inspection, multi-step research, persistent workspace use, and source-cited answers.
---

# Search as Code CLI v2

Compose searches, document inspection, and local processing in Python. Use `sdk.search` to find
sources, `sdk.search.many` for independent searches, and `sdk.content` to inspect evidence.
Keep intermediate data in the sandbox and return only what the next decision needs.

## Run a program

Pipe Python to the CLI in the outer shell:

```bash
opensac agent-run <<'PY'
from opensac_sdk import sdk

hits = sdk.search("Python 3.13 free threading", limit=5)
if hits is None:
    print("Search unavailable; inspect the OpenSAC warning.")
else:
    print("\n".join(f"{h['title']} — {h['source']}" for h in hits))
PY
```

Use a quoted heredoc. In an OpenSaC checkout, use `uv run opensac agent-run` if `opensac` is not on
`PATH`. The adapter manages conversation identity, REST sessions, and execution IDs; keep those
private. If neither launcher exists, or the adapter reports `context_*` or `configuration_error`,
report the setup failure and wait for correction.

The examples below are sandbox program bodies: adapt their constants and send their contents to
`agent-run` on stdin. Do not run them with host Python. Their workspace files live inside the
sandbox, not beside the example on the host.

## Search

`sdk.search(query, limit=5)` returns a list of hits or `None`. Each hit has `source`, `title`,
`snippet`, `domain`, and `date`. Pass the `source` string to content methods.
Snippets help select candidates; inspect document text before using it to support material claims.

This skill uses web search. Use `include_domains` to restrict sources. Choose queries for the
information the task needs.
Read [search parameters and hit fields](references/search.md) when composing searches.
See [web_search.py](examples/web_search.py) for a small candidate preview.

## Fan-out and saved results

Batch independent information needs in one program, then process the results locally:

```python
from opensac_sdk import sdk

queries = ["Python 3.13 free threading", "Python 3.13 JIT compiler"]
results = sdk.search.many(queries, limit=5)
rows, failed = [], []
for query, hits in zip(queries, results, strict=True):
    if hits is None:
        failed.append(query)
    else:
        rows.extend({"query": query, **dict(hit)} for hit in hits)
print(f"{len(rows)} hits; {len(failed)} failed queries.")
```

Results align with inputs, including failed positions. Deduplicate by `source` while retaining
query provenance. For ranked fusion, `sdk.search.fuse_rrf(queries, results, ...)` runs locally.

[fanout_research.py](examples/fanout_research.py) saves successful batches, merges candidates, and
loads them on later calls so completed searches are skipped. Files persist within a live session
when persistence is enabled; Python variables do not survive separate CLI programs. Use a different
artifact path for a different task. Empty results are completed searches, not operational failures.

Compose linked calls, filtering, joins, and rendering in one program. Split when inspecting an
observation could change the research strategy. Read [fan-out and state](references/fanout.md) when
combining rankings, resuming batches, or checking enumeration coverage.

## Inspect documents

Fetch selected pages, then locate and slice evidence locally. Full bodies stay in the program;
only relevant excerpts need to enter the model context.

```python
import re
from opensac_sdk import sdk

# sources contains a small set of URLs selected from search results.
documents = sdk.content.fetch_many(sources)
for source, document in zip(sources, documents, strict=True):
    if document is not None:
        text = document["text"]
        match = re.search("free.thread", text, re.IGNORECASE)
        if match:
            start, end = max(0, match.start() - 120), match.end() + 280
            print(f"{source}\n{text[start:end]}")
```

[snippets_verify.py](examples/snippets_verify.py) searches, fetches selected pages, and prints a
locally matched excerpt from each. Save and reuse bodies when later calls will inspect them again;
see [fanout_research.py](examples/fanout_research.py) for a save-and-resume example. Use string search for simple terms or Python `re` for
patterns. Change the local inspection when a new question arises instead of fetching the page again.
A keyword match locates evidence; it does not establish the exact claim. Preserve subject, relation,
and time scope when judging context. Expand the local excerpt when more context is needed.

Read [content methods](references/content.md) for interface details. Save bodies when later programs
will reuse them; repeated content requests still consume budget even when the page is cached.

## Choose further work and render results

Use existing sources and saved evidence to decide whether to inspect more text, broaden retrieval,
or answer. Batch searches and query variants are useful when they address distinct needs or a
specific retrieval gap. Reformulate from result feedback, ambiguity, or newly discovered terms;
repeated candidates alone do not justify another batch of synonyms.

Collect observations and print once where practical. Treat about 4,000 characters per invocation
as a soft ceiling, not a target. Print new evidence, changed conclusions, and actionable gaps;
retain source identifiers and enough context to evaluate each excerpt. Use plain-text summaries and excerpts by default; print JSON only when structured output helps
the next step. Save data only when a later call needs it, and report where omitted evidence can
be inspected. Do not repeat unchanged candidate lists or ledgers.

Stop when inspected evidence supports the requested answer and scope. Otherwise, choose an action
that could resolve a material gap. When useful next steps are exhausted or task constraints prevent
continuing, report the remaining uncertainty. The final answer should follow the full current state,
answer directly with source citations, and omit intermediate research details unless requested.

## Errors and common pitfalls

- Check `is None`: an empty list, string, or object can be a successful result. Batch failures remain
  input-aligned `None` entries. Read OpenSAC warnings and shell exit status alongside stdout.
- Results support mapping access and `dict(row)`; use key access for fields such as `items` or
  `values` that collide with mapping methods. Use `row["source"]` after JSON reload. There is no
  `.results` envelope around search hits. Content methods take source strings, not hit records.
- Let host policy manage retries and rate limits. Preserve failed inputs and retry only when the
  cause or available evidence warrants it. Local invalid arguments raise `ValueError`; configurable
  upper limits are enforced by broker policy.
- On `state_lost`, the program was not replayed: rebuild lost workspace data using source URLs.
  Adapter failures may leave execution unknown; inspect surviving progress before replaying.
- Adapter `HTTP 401` or `HTTP 403` is a host credential setup failure. Report the code and keep
  credentials private; do not configure authentication inside the program.

For a needed method's runtime documentation, read its `__doc__` directly, for example
`print(sdk.content.fetch.__doc__)`. The sandbox permits `__name__` and `__doc__` for dunder
introspection. Use ordinary Python for local computation and SDK methods for external operations.
