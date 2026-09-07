# Python SDK contracts

The SDK returns Pydantic models and ordinary lists. Model values support attribute access,
`model_dump()` and `model_dump_json()`. There is no HTTP response envelope.

| Method | Return value |
| --- | --- |
| `sdk.search(query_or_reformulations, limit=5)` | one `list[SearchHit]` |
| `sdk.search.many(queries, limit=5)` | `list[BatchItem[list[SearchHit]]]` |
| `sdk.content.fetch(url)` | `Document` |
| `sdk.content.fetch_many(urls)` | `list[BatchItem[Document]]` |
| `sdk.rerank(query, items, ...)` | `list[T]`, preserving original objects |
| `sdk.llm.complete(prompt, ...)` | `Completion` |
| `sdk.llm.complete_many(prompts, ...)` | `list[BatchItem[Completion]]` |
| `sdk.llm.extract(text, schema, ...)` | `Extraction` |
| `sdk.llm.extract_many(texts, schema, ...)` | `list[BatchItem[Extraction]]` |
| `sdk.capabilities()` | `Capabilities(methods, limits)` |
| `sdk.close()` | `None` |

SearchHit contains `url`, `title`, `snippet`, `domain`, `date`. List order expresses ranking.
`domain` defaults to the URL hostname, preserving subdomains, localhost or IP addresses. An explicit
provider value is retained. `date` is an optional provider-supplied string, including relative dates;
missing dates are `None`. Dates are not inferred or normalized.
Document contains only `url` and `text`.
Completion contains `text`, `model`, optional `usage`; Extraction contains Schema-validated
JSON `data`, `model`, optional `usage`. See [models](models.md) for fields and bounds.
Capabilities reports implemented methods and limits, not provider availability or credential validity.

Single-call expected failures raise OpenSACError with `code`, message, optional `status_code` and
`retryable`. Status codes are error metadata, not an HTTP response from OpenSaC. Retryable identifies
a transient category; calls are not retried automatically. Upstream bodies and credentials are excluded.

BatchItem has exactly one non-null `data` or `error`. Its read-only `ok` property is true on success,
including empty results; it is derived from `error` and is not an extra serialized field. ErrorInfo contains `code`, `message`, `retryable`.
Empty search lists are successful data. Duplicate inputs and input order are preserved. Invalid batch
parameters fail the whole call; expected provider failures affect their own items. Unexpected errors
and cancellation propagate.

Queries allow 1–2000 nonblank characters. A search accepts either one query string or 1–10
reformulations of one intent; reformulations are searched independently, fused locally and share
one result limit. Search limit is 1–100; batches contain 1–32 independent inputs.
Document URLs must be HTTP(S), at most 8192 characters; public and local service URLs share this
contract. Fetch needs no preceding search or registration. Provider and runtime configuration use
`opensac.config.Settings` or corresponding `OPENSAC_*` environment variables.

Rerank providers return input positions and scores. Runtime checks those positions, and the SDK
returns the selected original objects. Provider details and HTTP backend request shapes are in
[provider authoring](providers.md); these endpoints belong to backend services, not OpenSaC.

Async Runtime methods use the same ordinary arguments, for example `await runtime.search(query)`,
`await runtime.search([primary, alternate])` and `await runtime.extract(text, schema)`. No request
model needs to be constructed by the caller.
Runtime rerank returns validated provider indices/scores; SDK rerank maps them to original objects.

```python
for r in sdk.search.many(["first query", "second query"]):
    if r.ok:
        print(r.data)
    else:
        print(r.error)
```

## Error types

All exception definitions and ErrorInfo live in `opensac.errors`. Built-in failures use specific
subclasses, while `except OpenSACError` still catches them all. Expected capability failures inherit
CapabilityError and become ErrorInfo in batch results; exception objects are not serialized.

| Category | Types |
| --- | --- |
| Input | InvalidRequestError; InvalidSchemaError inherits it |
| Configuration | ConfigurationError; NotConfiguredError inherits it |
| Lifecycle | ClientClosedError, RuntimeClosedError |
| Runtime deadline | RequestTimeoutError |
| Backend | ProviderError, ProviderTimeoutError, ProviderUnavailableError, ProviderRateLimitError |
| Backend output | ProviderResponseError, ResponseTooLargeError |
| Model output | ModelRefusalError, GenerationIncompleteError, StructuredOutputError |

All backend and model-output types above inherit ProviderError. Class defaults supply the stable
error code, status and retryable flag; the message describes the individual failure. Specific errors
accept `message` and an optional `retryable` override. `NotConfiguredError` denotes missing required
provider configuration; ConfigurationError also covers provider loading and initialization failures.

```python
from opensac.errors import ProviderRateLimitError, ProviderError

try:
    hits = sdk.search("query")
except ProviderRateLimitError as error:
    print("Rate limited:", error.code)
except ProviderError as error:
    print("Provider failed:", error.code)
```

Custom providers can continue raising `CapabilityError` with their own codes. Such generic exceptions
are not automatically converted into typed subclasses by inspecting the code string. Batch callers
use `r.ok` and `r.error.code` to distinguish outcomes.
