---
name: search-as-code
description: Compose OpenSAC search primitives as Python programs.
---

# Search as Code

Use Python for orchestration and `opensac_sdk` for every external capability.

```python
from opensac_sdk import sdk
```

## Primitives

- `sdk.search.web(query, limit=10, domains=None)`
- `sdk.search.local(query, limit=10)`
- `sdk.search.web_many(queries, limit_per_query=10, concurrency=5)`
- `sdk.search.local_many(queries, limit_per_query=10, concurrency=5)`
- `sdk.content.get_many(refs)`
- `sdk.content.snippets(query, refs, max_tokens=4000, max_tokens_per_page=1000)`
- `sdk.llm.complete(prompt, system=None, temperature=0.2, max_tokens=None)`
- `sdk.llm.complete_many(prompts, concurrency=4, ...)`
- `sdk.llm.extract_many(items, instruction=..., schema=..., concurrency=4)`
- `sdk.state.read_json/read_jsonl` and `write_json/write_jsonl`
- `sdk.output.submit(output, citations=[{"ref": hit.ref}])`

Search returns typed hits with `ref`, `backend`, `title`, `url`, `docid`, `domain`,
`snippet`, `score`, and `rank`. Content calls accept only opaque refs returned during
the current session.

A `ref` is stable for the document behind it: the same page or document returned by two
different queries comes back as the same `ref`, so `{h.ref: h for ...}` deduplicates a
multi-query candidate pool without comparing URLs by hand.

## Session capabilities

The primitive list above is the full set. A session may have some of it switched off for
an experiment, in which case the disabled calls raise with a message saying so and how to
work around it — batching may be limited to one item per call, `sdk.llm.*` may be
unavailable, and the workspace may not survive across turns. Read the session's
`capabilities` list rather than assuming, and when a call reports a disabled mechanism,
restructure around it instead of retrying the same call.

> Hosts that generate their own skill text: build the primitive list from the session's
> `capabilities` field, not from a copy of this file. Naming a capability the session
> cannot reach costs the model a turn to discover.

## Strategy

Fan out independent queries with `*_many`. Encode source constraints in queries and
`domains` before retrieval. Deduplicate with ordinary Python before fetching content.
Fetch only promising candidates. Use deterministic code for regex, joins, filtering,
counting, ranking, and coverage checks. Use `llm.extract_many` for semantic work with a
fixed shape, and `llm.complete` only for planning steps whose output has no schema, such
as summarizing current coverage and proposing follow-up queries. Validate anything
`llm.complete` returns with code before acting on it.

Persist compact intermediate records to JSONL when later turns may need them. The
workspace and the ref table survive across turns, so refs written to JSONL in one turn
still resolve in a later one. Submit only evidence and summaries useful to the control
model, not every raw result.

Never use direct HTTP, sockets, subprocesses, shell commands, credentials, environment
inspection, or package installation. Dunder attributes are rejected apart from `__name__`
and `__doc__`, so report errors with `type(exc).__name__` and never introspect via
`__class__` or `__dict__`. Citations must contain opaque refs returned by search;
the broker resolves their trusted URL, document ID, title, and evidence. Never invent refs.

## Pattern

```python
from opensac_sdk import sdk

batches = sdk.search.web_many(queries, limit_per_query=8, concurrency=6)
hits = {h.url: h for batch in batches for h in batch.hits if h.url}
pages = sdk.content.snippets(goal, [h.ref for h in hits.values()])
records = sdk.llm.extract_many(
    [p.model_dump() for p in pages],
    instruction=instruction,
    schema=schema,
)
sdk.state.write_jsonl("records.jsonl", records)
sdk.output.submit({"records": records})
```
