# Search reference

Read when composing a search, choosing filters, or inspecting hit fields.
Import `sdk` from `opensac_sdk`. For a runnable candidate preview, see
[web_search.py](../examples/web_search.py).

## Common parameters

The call below shows commonly used options, not the full SDK signature.

```text
sdk.search(query, *, limit=10, include_domains=None) -> list[record] | None
```

- `query` is one string. Use `sdk.search.many` for a list of queries; see [fanout.md](fanout.md).
- `limit` controls the requested number of hits.
- `include_domains` is an optional list of domain strings that restricts web search to those domains.

## Hit fields

The result is a bare list. Useful fields are `source`, `title`, `snippet`, `domain`, and `date`.
Keep original hit records when saving or passing results to SDK helpers; other fields need not be
printed or interpreted.

`source` is the web URL used for content calls and joins; see [content.md](content.md).

Treat snippets as candidate-selection data. Ranking and repeated appearances do not establish
that a document supports the requested claim. Inspect relevant text before citing material facts.
