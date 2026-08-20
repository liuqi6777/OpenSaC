# OpenSAC v0.6.3

OpenSAC v0.6.3 makes source URLs the public document-addressing contract. Internal document IDs
still exist for registry and cache bookkeeping, but generated programs only exchange URL or local
document-ID strings.

## SDK changes

- `sdk.content.*` accepts `list[str]` only. Passing search records directly is no longer supported.
- A web deployment can read a bounded public HTTP(S) URL without requiring the URL to have appeared
  in the same `sac_run`. Set `OPENSAC_CONTENT_URL_ADMISSION=searched_only` to require prior search.
- Local document IDs remain search-admitted and session-bound.
- `sdk.citations` and `citations.resolve` are removed. Content results no longer expose `locator` or
  `locator_error`.
- `sdk.output.submit(..., citations=[...])` retains `citations` as an optional `list[str]` of
  lightweight, unverified source labels. Output submission performs no broker lookup or citation
  validation.

These changes are intentionally incompatible with the previous sandbox RPC surface. The sandbox
contract is now `9`, and the capability contract is `8`; use matching v0.6.3 service and sandbox
images.

## Retrieval reliability

- Content failures such as provider timeouts and malformed provider responses are returned as
  input-aligned failure rows instead of failing an otherwise useful batch.
- `OPENSAC_CONTENT_BATCH_DEADLINE_SECONDS` bounds a full content batch. Unfinished rows return
  `content_deadline_exceeded`, while completed rows are preserved.
- Provider attempt timeouts and logical deadlines can be overridden per operation with
  `OPENSAC_PROVIDER_OPERATION_ATTEMPT_TIMEOUT_SECONDS` and
  `OPENSAC_PROVIDER_OPERATION_LOGICAL_DEADLINE_SECONDS` JSON maps.
- Internet Archive detail pages can fall back to the archive's bounded plain-text representation.
  Each fallback remains separately visible in provider-attempt accounting.

## Source identity and observability

- URL canonicalization is conservative: it removes fragments and known tracking parameters without
  decoding and re-encoding path data.
- Direct URL admission rejects malformed URLs, local hostnames, userinfo, and non-public IP
  literals before provider work.
- Capability traces record whether a document was admitted by `search` or `direct_url` without
  exposing an internal source ID. Session usage reports direct URL attempts and successful
  admissions.

## Upgrade example

```python
from opensac_sdk import sdk

source_url = "https://example.org/report"
passage = sdk.content.read([source_url], offset=1, limit=40)[0]
sdk.output.submit(
    {"source": passage.source, "excerpt": passage.text[:1000]},
    citations=[passage.source],
)
```

Search records remain useful for ranking and selection, but pass `hit.source` rather than the
record itself when reading content.
