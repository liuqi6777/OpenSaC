# Content reference

Read when fetching bodies, locating text, or following document windows. See
[snippets_verify.py](../examples/snippets_verify.py) for search followed by passage inspection.
All content methods take source strings, not search-hit records.

## Fetch bodies

```text
sdk.content.fetch(source) -> record | None
sdk.content.fetch_many(sources, *, concurrency=5) -> list[record | None]
```

Common document fields are `source`, `text`, `title`, and `date`.
`fetch_many` returns one aligned position per input source; preserve failures as described in
[fanout.md](fanout.md).

Save bodies with their source URLs when later calls will reuse them. Work on saved text instead
of fetching it again for another local inspection.

## Locate passages

```text
sdk.content.grep(
    pattern, *, sources, mode="regex", case_sensitive=False,
    start_line=1, context_lines=0, limit_per_source=20
) -> list[record | None]
```

`sources` is a list of source strings. `mode` accepts `regex` or `literal`. Each successful result
contains `source`, `title`, `matches`, and `next_start_line`. Each match contains `line`, `text`,
`before`, `after`, and `spans`. `before` and `after` are lists of surrounding line strings.
Each span has 0-based, end-exclusive `start_character` and `end_character` within the matching line.

Matching runs separately on each line; patterns cannot match across line boundaries.
`limit_per_source` caps matching lines, not individual matches. Multiple matches on one line
appear in that line's `spans`.

For capped scans, continue each source from its non-null `next_start_line`. A null cursor means the
source was scanned to EOF. Empty matches are a successful result, not a failed operation.

## Read context

```text
sdk.content.read(
    source, *, start_line=1, start_character=0, line_count=200, max_chars=100_000
) -> record | None
```

The result contains fetched-document fields plus `window`. Window fields are `start_line`,
`start_character`, `end_line`, `end_character`, `total_lines`, `next`, and `truncated_by_max_chars`.
Lines are 1-based; character positions are 0-based and end-exclusive.

`window.next` is `None` at EOF or the exact `start_line`/`start_character` for a lossless follow-up.
Pass both coordinates when continuing: `max_chars` may stop within one long line.

```python
# page is a successful read result; continue only when more context is needed.
if page["window"]["next"] is not None:
    following = sdk.content.read(source, **page["window"]["next"], line_count=20, max_chars=2000)
```

## Reuse and evidence

Use source URLs for document access. Each source requested through `fetch`, `read`, or `grep`
consumes content-fetch budget even when the page is cached. When the body is already saved,
locate and slice text locally for repeated checks.

Keep excerpts beside their source and coordinates so their context can be revisited.
