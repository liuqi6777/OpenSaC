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

- `sdk.search.web(query, limit=10, offset=0, domains=None)`
- `sdk.search.local(query, limit=10, offset=0)`
- `sdk.search.web_many(queries, limit_per_query=10, offset=0, concurrency=5)`
- `sdk.search.local_many(queries, limit_per_query=10, offset=0, concurrency=5)`
- `sdk.content.get_many(refs)`
- `sdk.content.snippets(query, refs, max_tokens=4000, max_tokens_per_page=1000)`
- `sdk.content.grep(refs, pattern, context=0, max_matches_per_ref=20)`
- `sdk.content.read(refs, offset=1, limit=200)`
- `sdk.citations.resolve(refs)`
- `sdk.llm.complete(prompt, system=None, temperature=0.2, max_tokens=None)`
- `sdk.llm.complete_many(prompts, concurrency=4, ...)`
- `sdk.llm.extract_many(items, instruction=..., schema=..., concurrency=4)`
- `sdk.state.read_json/read_jsonl`, `write_json/write_jsonl`, `append_jsonl`, `exists`,
  `list(prefix="")`
- `sdk.session.usage()` — searches, fetches and LLM calls made so far
- `sdk.output.submit(output, citations=[{"ref": hit.ref}])`

Search returns typed hits with `ref`, `backend`, `title`, `url`, `docid`, `domain`,
`date`, `snippet`, `score`, and `rank`.

A `ref` is stable for the document behind it: the same page or document returned by two
different queries comes back as the same `ref`, so `{h.ref: h for ...}` deduplicates a
multi-query candidate pool without comparing URLs by hand. Anywhere a `ref` is accepted
you may equally pass a `docid` or a `url` from a hit you already hold — whichever is
easiest to carry — but only for documents a search in this session actually returned.
Handles cannot be invented or guessed: retrieval is the one door into the corpus.

`offset` is depth into the ranking, and it decides more than convenience. A document
becomes readable only by being returned from a search, so `limit` is at once how far
you can see and how far you are allowed to reach. If you believe the answer sits below
the first page, ask for it with `offset` rather than rewriting the query. Backends differ
in how deep they go: the local corpus has no ceiling, while web search serves at most rank
100 and refuses a deeper request rather than clipping it, so a program is never told it
read a rank that nothing looked at.

Reading a document has four shapes, and the last two are what let you choose the passage
instead of accepting one:

- `get_many` — the whole text.
- `snippets` — one window per page, chosen by a broker-side scorer against your query.
- `grep` — matching lines across many documents, each with its 1-indexed line number.
- `read` — a line window; `metadata` carries `start_line`, `end_line`, `total_lines`, and
  `next_offset` (`None` at the end).

A `ContentMatch.line` is a `read(offset=...)` directly, so locating and reading compose
with no character arithmetic. Most corpus documents are far longer than anything worth
printing, so `grep` then `read` is usually cheaper and more reliable than `get_many`.
Because a line is a sentence in some corpora and a whole section in a scraped web page,
`read` bounds its window by characters as well as by lines and says so in `metadata`.

A document is retrieved once per session and cached, so grepping and re-reading a pool you
already fetched costs nothing further; `content_fetches` and `content_backend_fetches` are
both reported so the saving is visible rather than hidden. Every content call returns one
row per handle requested, in order. A page that could not be retrieved — a paywall, a
robots block, a timeout — comes back with empty `text` and `metadata["fetch_error"]`
rather than being dropped, so a short result is never mistaken for a complete one. Only if
*every* document in a call fails does the call raise, since that is infrastructure rather
than a property of the pages.

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

The sandbox is a computer, not a slower way to call one tool. One execution should carry a
whole stage of research — fan out, filter, read, extract, report — because a round trip is
paid for each one, and only what the program prints or submits survives it. A program that
performs a single action and stops is the most expensive way to use this interface.

Three stages usually finish a question, and they do not each need their own turn when the
work is already determined:

1. **Survey.** Fan out independent queries with `*_many`, encoding source and date
   constraints into the queries (and `domains`) before retrieval rather than filtering
   afterwards. Merge into one pool keyed by `ref`. Print rank, handle, date and title —
   one line each, no snippets — and persist the pool with `state.write_jsonl`.
2. **Locate.** `grep` the whole pool for the distinguishing strings: names, dates,
   numbers, quoted phrases. `read` a window around the matches worth following. This is
   where the answer is found, and it is the step most often skipped.
3. **Verify.** Check the candidate against every constraint in the question, in code
   wherever the constraint is mechanical. Submit the passage that settles it, with its
   handle as a citation.

Use deterministic code for regex, joins, filtering, counting, ranking, set arithmetic and
coverage checks — none of it needs a capability, and writing it inline is more precise
than a primitive that would generalise it. Use `llm.extract_many` for semantic work with a
fixed shape, and `llm.complete` only for planning steps whose output has no schema, such
as summarizing current coverage and proposing follow-up queries. Validate anything
`llm.complete` returns with code before acting on it.

Defaults worth starting from: `limit_per_query=10`, then `offset=10` on a query that
looked promising (ranks 11–20 of a query that half worked usually beat a fresh phrasing of
one that failed outright); `concurrency=6` on a fan-out; `grep` the whole pool at once
with `context=2`; `read(offset=match.line - 10, limit=40)` around a promising match.

Four things go wrong often enough to name. Answering from search snippets: a snippet is a
retrieval preview chosen by the index, not evidence. Searching again instead of reading:
once a document is in the pool, grepping it costs nothing, while rewriting the query is
the expensive move. Dumping: printing raw snippets or whole pages fills the caller's
observation budget with material the program already held. Losing the pool: a handle
neither printed nor written to the workspace is gone when the program exits.

Persist compact intermediate records to JSONL when later turns may need them. The
workspace and the ref table survive across turns, so handles written to JSONL in one turn
still resolve in a later one: extend a record with `append_jsonl` rather than reading and
rewriting it, and call `exists` before assuming an earlier turn left something behind.
`session.usage()` reports how much has been retrieved so far. Nothing rations it — there
are no ceilings — so it is there for a program that wants to pace itself: retrieval
climbing while evidence does not is the signal to read rather than search again. Submit
only evidence and summaries useful to the control model, not every raw result.

Never use direct HTTP, sockets, subprocesses, shell commands, credentials, environment
inspection, or package installation. Dunder attributes are rejected apart from `__name__`
and `__doc__`, so report errors with `type(exc).__name__` and never introspect via
`__class__` or `__dict__`. Citations must name a document search returned; the broker
resolves its trusted URL, document ID, title, and evidence.

## Pattern

```python
from opensac_sdk import sdk

batches = sdk.search.web_many(queries, limit_per_query=10, concurrency=6)
hits = {h.ref: h for batch in batches if not batch.error for h in batch.hits}
for ref, hit in hits.items():
    print(f"{hit.rank} {ref} {hit.date or ''} {hit.title}")

matches = sdk.content.grep(list(hits), r"born in (18|19)\d{2}", context=2)
records = sdk.llm.extract_many(
    [m.model_dump() for m in matches],
    instruction=instruction,
    schema=schema,
)
sdk.state.write_jsonl("records.jsonl", records)
sdk.output.submit({"records": records}, citations=[{"ref": m.ref} for m in matches[:5]])
```
