# Capability providers

Search, fetch, rerank and LLM are independent providers. There is no combined provider and no web/local category.
The runtime loads installed Python entry points on first capability use; adding a provider requires installing its
package and selecting its name in configuration, not editing a built-in registry.

| Capability | Entry-point group | Operation |
| --- | --- | --- |
| search | `opensac.search` | `async search(query: str, limit: int) -> list[SearchHit]` |
| fetch | `opensac.fetch` | `async fetch(url: str) -> Document` |

Each provider also implements `async aclose()`. Rerank and LLM use separate contracts/groups described in [model providers](models.md).

## Register a search provider

In an independently installable Python package:

```toml
[project.entry-points."opensac.search"]
my-search = "my_search:Search"
```

Its entry point references a synchronous factory or class accepting `(config, context)`:

```python
from opensac.contracts import SearchHit
from opensac.provider import ProviderConfig, ProviderContext


class Search:
    def __init__(self, config: ProviderConfig, context: ProviderContext):
        # Validate provider-specific config and allocate local resources here.
        # Do not make network requests during construction.
        self.config = config
        self.context = context

    async def search(self, query: str, limit: int) -> list[SearchHit]:
        # Delegate to the search system. It must return document URLs.
        ...

    async def aclose(self) -> None:
        # Close resources owned by this provider.
        ...
```

The snippet defines the contract, not a runnable search implementation. A search provider does not
implement fetch. A fetch provider registers its own entry point and does not implement search.
Multiple providers can be shipped by one distribution, but each capability factory is independent.

Install the provider into the caller's Python environment, then configure:

```bash
OPENSAC_SEARCH_PROVIDER=my-search
OPENSAC_SEARCH_BASE_URL=https://search.example/api
OPENSAC_SEARCH_API_KEY=...
OPENSAC_SEARCH_OPTIONS='{"collection":"papers"}'
OPENSAC_FETCH_PROVIDER=jina
OPENSAC_FETCH_API_KEY=...
```

Create a fresh runtime/process to
discover changes; in-flight replacement/hot reload is not implemented. Duplicate entry-point names
in the same group and unknown selected names fail at capability initialization instead of selecting arbitrarily.
Provider credentials and configuration belong to the caller’s environment.

## Contracts

`ProviderConfig` contains base_url, api_key (SecretStr), model, max_tokens, temperature and options
(a JSON object). LLM providers read generation parameters from this configuration.
`ProviderContext` contains the request deadline and maximum provider-response bytes. Built-in HTTP
providers honor the response bound; custom providers must also bound their own I/O. The shared runtime
enforces its operation timeout and concurrency around all providers, and caps search results to the
requested limit. Returned model instances are revalidated, including nested token usage; invalid
updates or unchecked model construction cannot bypass the result contract. Malformed HTTP response
encoding and excessively nested JSON become `ProviderResponseError` and remain per-item failures in
batches. Provider initialization failures are exposed as `configuration_error` (503) at the
runtime boundary and cached as failures until a fresh runtime. Shared provider instances close once.

Search reformulations are runtime composition, not an additional provider operation. Passing a list
to `sdk.search` invokes the single-query provider once per distinct string, fuses rankings locally
and applies one total result limit. Provider authors only implement `search(query, limit)`.

Return `SearchHit(url, title, snippet, domain, date)` or `Document(url, text)`. URLs must already
exist at the backend. Do not invent document identifiers or add a registration dependency. A fetch
provider may support internal URLs, public URLs, or both, according to its actual implementation.
Keep the requested URL as Document.url and report errors explicitly.

Import errors from `opensac.errors`. Prefer a specific type such as
`ProviderTimeoutError("Provider timed out.")`; use `CapabilityError(code, message, status, retryable)`
for custom expected failures. Do not put upstream
response bodies, credentials, or private document contents in error messages. Batch failures are
rendered per input; cancellation must propagate so resources can be released.

Provider code executes in the process hosting the runtime. Providers are environment-installed code,
not packages chosen per request. The provider ABI is currently experimental (`0.1.0.dev0`); specify
compatible opensac versions when publishing an external provider.

## Built-ins

- `opensac.search:serper`: Serper search API, using search-specific config.
- `opensac.fetch:jina`: Jina Reader with direct file handling and source-download fallback.
- `opensac.search:http`: POST `<base_url>/search` with query/limit; expects `{"data": [SearchHit, ...]}`.
- `opensac.fetch:http`: POST `<base_url>/fetch` with url; expects `{"data": Document}`.

The two HTTP providers work with independent endpoints and credentials. They are URL-native capability
adapters, not adapters for a legacy document-ID service. A service can implement either capability
without exposing the other.

## Jina file handling and fallback

The default fetch provider automatically downloads URLs whose path ends in `.pdf`, `.json`, or
`.csv` (case-insensitive, ignoring query parameters). Other URLs use Jina Reader first; request
failures, rate limits, timeouts, oversized responses, and invalid or empty Reader text trigger one
source download. No preliminary HEAD request or retry loop is added. HTTP and third-party fetch
providers retain their own behavior.

Downloads follow at most five HTTP(S) redirects, preserve the requested `Document.url`, and never
send the Jina API key to the source. PDF signatures take precedence over metadata; concrete response
media types take precedence over filename suffixes. Missing or generic media types use the final
URL suffix, PDF signature, or an HTML document prefix. A suffix-free file uses local parsing only
if Reader fails and a source download occurs.

PDF text is extracted locally with pypdf and joined in page order with blank lines. Password-protected
PDFs that cannot be opened with an empty password, damaged PDFs, and PDFs without extractable text
raise `ProviderResponseError`; OCR is not included. JSON and CSV are decoded using the declared
charset (UTF-8 by default) and returned without parsing, reformatting, or trimming. HTML is converted
to plain text with Trafilatura, excluding comments; cleaned page text is used when body extraction
fails to find content. Other explicit text media types are preserved; unknown binary content and
invalid text encodings raise `ProviderResponseError`.

Reader receives at most half of `request_timeout`, reserving time for fallback. One full deadline
covers Reader, source download, and parsing, including direct provider calls; Runtime retains its
own operation deadline and concurrency admission. Downloads enforce `max_response_bytes` on
decompressed bytes, and extracted UTF-8 text has the same limit. Local parsing runs in a worker
thread; cancelling the wait does not forcibly stop an already running parser.

Direct file failures are returned immediately. If both Reader and fallback fail, the source-stage
error is raised with the Reader error chained as its cause. Public errors omit upstream response
bodies and credentials. Configuration errors, external cancellation, and unexpected exceptions
propagate without fallback. Both HTTP clients are released by `aclose()`.

## Built-in module layout

`opensac.provider` defines capability protocols, configuration and entry-point loading.
`opensac.providers` contains implementations grouped by service or protocol:
`serper`, `jina`, `http` and `openai`; `direct` supports Jina source downloads and extraction.
The `base` module shares bounded
HTTP transport helpers. Sharing a module does not combine capability interfaces: HTTP search,
fetch and rerank remain separate classes and entry points.

## Options and local diagnostics

Built-ins validate options before allocating an HTTP client and before sending requests. Options
must be JSON data with finite numbers. Their behavior is capability-specific:

| Provider | Options | Protected fields |
| --- | --- | --- |
| Serper search | `gl` and `hl`, both nonempty strings; forwarded in the search body | `q`, `num` |
| Jina fetch | No options supported; nonempty options raise ConfigurationError | All options rejected |
| HTTP search | Extra JSON body fields, validated by the configured backend | `query`, `limit` |
| HTTP fetch | Extra JSON body fields, validated by the configured backend | `url` |
| HTTP rerank | Extra JSON body fields, validated by the configured backend | `model`, `query`, `documents`, `top_n` |
| OpenAI-compatible LLM | Extra JSON body fields, validated by the configured backend | Managed generation, message, schema and tool fields listed in `OpenAILLM.reserved_options` |

For example, `OPENSAC_SEARCH_OPTIONS='{"gl":"us","hl":"en"}'` selects Serper's country and language
fields. The adapter's supported subset is deliberately explicit; other Serper options fail locally.
The HTTP adapters forward extension fields without claiming that a particular backend supports them.

`sdk.check_configuration()`, `client.check_configuration()` and `runtime.check_configuration()`
return per-capability ConfigurationCheck records. Discovery failures and missing built-in settings
are reported without constructing instances or sending requests. This is a local check, not a
credential, connectivity or model-availability probe.

External factories may expose an optional synchronous `validate_config(config)` function or
classmethod accepting ProviderConfig and returning None. It must perform no network I/O or resource allocation, and should
raise ConfigurationError/NotConfiguredError with a safe, actionable message on failure. Factory
imports must also be offline. Factories without this hook are reported as `unverified` rather than
assumed valid. Unexpected import/validator exceptions are sanitized. Checks do not initialize or
cache providers and do not consume their lazy initialization attempts. Explicitly injected Runtime
providers are marked `provider="injected", status="unverified"`; settings for the replaced capability
are not checked because they are not used to create that instance.

## Initialization and cleanup ownership

Factories remain synchronous `(config, context)` callables. The low-level Python helper
`load_provider(...)` is now asynchronous and must be awaited so it can finish disposing an instance
that fails interface validation. If a rejected instance exposes async `aclose()`, cleanup finishes
before initialization reports failure; cancellation during this disposal is deferred until it ends.
Factories are responsible for releasing resources if their constructor raises before returning an
instance, and every returned instance must provide async `aclose()`.

Runtime serializes initialization within each capability and caches failures until a fresh runtime.
Concurrent `aclose()` callers share a cleanup task; cancelling a waiter does not cancel cleanup.
A subsequent `aclose()` can join the task. Providers shared across capabilities are closed once,
and one provider's cleanup failure does not prevent attempts to close the others.
