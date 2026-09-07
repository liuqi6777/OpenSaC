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
requested limit. Provider initialization failures are exposed as `configuration_error` (503) at the
runtime boundary and cached as failures until a fresh runtime. Shared provider instances close once.

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
- `opensac.fetch:jina`: Jina Reader API, using fetch-specific config.
- `opensac.search:http`: POST `<base_url>/search` with query/limit; expects `{"data": [SearchHit, ...]}`.
- `opensac.fetch:http`: POST `<base_url>/fetch` with url; expects `{"data": Document}`.

The two HTTP providers work with independent endpoints and credentials. They are URL-native capability
adapters, not adapters for a legacy document-ID service. A service can implement either capability
without exposing the other.

## Built-in module layout

`opensac.provider` defines capability protocols, configuration and entry-point loading.
`opensac.providers` contains implementations grouped by service or protocol:
`serper`, `jina`, `http` and `openai`. The `base` module shares bounded
HTTP transport helpers. Sharing a module does not combine capability interfaces: HTTP search,
fetch and rerank remain separate classes and entry points.
