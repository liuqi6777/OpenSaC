# Broker plugin development

OpenSAC assembles broker backends and capability modules in the broker layer. The API runtime only
asks `BrokerBuilder` for a complete assembly; it does not import individual providers, capability
classes, limits, or request models.

This guide describes the extension model implemented today. The important distinction is:

- Backend providers are a supported third-party plugin surface.
- Capability discovery is currently a core-development mechanism. It is not yet a general-purpose
  third-party replacement mechanism because the public RPC contract is closed and built-in module
  names cannot be overridden.

Broker plugins run as trusted Python code in the OpenSAC host process. They can read host
credentials and access host resources; they are not sandboxed. Install only reviewed packages.

## Assembly and discovery

The assembly path is:

```text
installed entry point or embedded BrokerPlugin
                    |
             explicit modules
                    |
      decorated factories / concrete classes
                    |
       BackendCatalog + CapabilityCatalog
                    |
              BrokerBuilder
                    |
    BackendBinding + ProviderExecutor + capabilities
                    |
     BrokerService + BrokerRuntime + manifest
```

Installed packages register the `opensac.broker_plugins` entry-point group. An entry point may
load one of three things:

1. a module, discovered with `BrokerPlugin.from_modules(module)`;
2. a `BrokerPlugin` object; or
3. a zero-argument function returning a `BrokerPlugin`.

Discovery is deliberately bounded. OpenSAC inspects only modules explicitly loaded by an entry
point or passed to `BrokerPlugin.from_modules()`. It does not walk `sys.path`, import every module
in a package, or inspect the global `__subclasses__()` graph. Imported classes and functions are
ignored; a discovered object must be declared in the inspected module itself.

For a plugin split across multiple modules, expose a small composition function:

```python
# example_opensac/plugin.py
from opensac.broker import BrokerPlugin

from . import backends, capabilities


def create_plugin() -> BrokerPlugin:
    return BrokerPlugin.from_modules(backends, capabilities)
```

```toml
# pyproject.toml
[project.entry-points."opensac.broker_plugins"]
example = "example_opensac.plugin:create_plugin"
```

The current Python plugin API version is `1`. `BrokerBuilder` rejects a plugin with a different
`BrokerPlugin.api_version` during startup.

## Add a backend provider

Backends use structural protocols rather than a shared base class because search, document,
rerank, and LLM adapters have different contracts. A factory opts into discovery with
`@backend_provider`:

```python
# example_opensac/backends.py
from opensac.backends import BackendBuildContext, backend_provider

from .search import AcmeSearchBackend


@backend_provider(role="search", name="acme")
def create_acme_search(context: BackendBuildContext) -> AcmeSearchBackend:
    config = context.settings.backends.search
    return AcmeSearchBackend(
        endpoint=str(config.options["endpoint"]),
        timeout=context.timeout("search"),
    )
```

The decorator's `name` is the provider selector used in YAML. The backend instance's `name` is a
logical retrieval route. Search and document providers selected for one deployment must return the
same non-empty route name, even when their provider selectors differ.

```yaml
backends:
  search:
    provider: acme
    options:
      endpoint: https://search.example.test
  document:
    provider: acme_reader
    options:
      endpoint: https://reader.example.test
```

Each selected factory receives a `BackendBuildContext` containing validated application settings
and provider execution policies. Use `context.timeout(service)` to obtain the configured attempt
timeout instead of re-reading policy settings. Provider-specific, non-secret values belong under
the corresponding `options` mapping. Secret-looking keys are rejected recursively in `options`;
read credentials from plugin-owned environment variables or another host secret mechanism.

The factory result is checked at startup:

| Role | Required fields | Required methods |
| --- | --- | --- |
| `search` | `name`, `provider_identity`, `result_cacheable`, `supports_domains`, `max_depth` | async `search(...)` |
| `document` | `name`, `provider_identity`, `result_cacheable`, `source_kind` | `fetch_candidates(...)`, async `fetch(...)` |
| `rerank` | `name`, `provider_identity` | `preflight()`, async `rerank(...)` |
| `llm` | `name`, `provider_identity` | async `complete(...)` |

Use the normalized models exported by the relevant package:

- `opensac.backends.search`: `SearchHit` and `RetrievalMetadata`;
- `opensac.backends.document`: `DocumentHandle` and `DocumentContent`;
- `opensac.backends.rerank`: `RerankScore`; and
- `opensac.backends.llm`: `LLMResponse`.

`provider_identity` is an opaque process-wide governor identity. It must distinguish effective
provider endpoints and credentials without exposing credential material in traces or logs. An
adapter may additionally implement async `aclose()`; the broker will close supported backends when
it stops.

Adding another provider within one of the four existing roles does not require changing
`BrokerBuilder`, the API runtime, or `CAPABILITY_CONTRACT`. Adding a fifth backend role is a core
architecture change because policy construction, assembly, bindings, and configuration all need
to understand that role.

### Backend execution model

The backend protocol is also the operation contract. There is no parallel `ProviderOperation`
abstraction and no search/document/rerank/LLM service class hierarchy. `BrokerService` pairs each
backend with its host-owned `ProviderRuntime` in a small typed `BackendBinding`, then gives those
bindings to capabilities.

Capabilities own the semantics that differ by method: request fingerprints, normalized output
validation, candidate fallback, and response shaping. Every actual provider call goes through the
single `ProviderExecutor.execute()` path, which owns concurrency, retries, deadlines, caching,
in-flight coordination, cancellation, accounting, and tracing. A new provider for an existing role
therefore adds a backend implementation and factory, not another operation or service wrapper.

### Add a built-in backend

Put the adapter in the matching `src/opensac/backends/<role>/` package and add its decorated factory
to `src/opensac/backends/builtin.py`. That module is already part of the built-in `BrokerPlugin`, so
no catalog or API-runtime edit is needed. Add provider configuration and focused catalog/capability
tests with the adapter.

## Add a capability module

A capability module inherits `BaseCapabilities`; its async handlers use `@capability_method`.
Keep its request models, limits/configuration models, trace helpers, and implementation in the same
capability module unless another component genuinely shares them.

```python
# src/opensac/broker/capabilities/analyze.py
from typing import Any, Self

from pydantic import Field

from opensac.broker import BaseCapabilities, capability_method
from opensac.broker.registry import CapabilityRequest
from opensac.broker.session import BrokerSession


class AnalyzeRequest(CapabilityRequest):
    text: str = Field(min_length=1)


class AnalyzeCapabilities(BaseCapabilities):
    name = "analyze"

    @classmethod
    def from_context(cls, context: Any) -> Self:
        # Select only the broker-owned bindings/configuration this module needs.
        del context
        return cls()

    @capability_method("analyze.run", AnalyzeRequest)
    async def run(
        self,
        state: BrokerSession,
        request: AnalyzeRequest,
    ) -> dict[str, str]:
        del state
        return {"text": request.text}
```

`from_context()` receives a `CapabilityBuildContext` with:

- the shared provider executor;
- search and document backend-binding mappings;
- rerank and optional LLM backend bindings;
- broker-owned capability configuration;
- default provider concurrency; and
- the session-manifest callback.

`BaseCapabilities.specs()` turns decorated handlers into `CapabilitySpec` objects. A handler's RPC
family must match the module name (`analyze.*` belongs to `name = "analyze"`). Optional trace hooks
may be passed to any `@capability_method` as `trace_queries`, `trace_input_count`, and
`trace_result_count`. Each accepts a callable or instance-method name and only populates that
method's trace event; it does not change validation or execution. Override `manifest()` only when
the module has deployment facts or limits to advertise.

For a new built-in module, add the module to the explicit list in `builtin_broker_plugin()` in
`src/opensac/broker/plugins.py`. This is the one intentional composition point: adding a file does
not cause arbitrary package imports. If the module needs deployment configuration, keep its
configuration model beside the module and make `BrokerConfig` compose that model.

### Current capability-plugin boundary

The loader can discover a concrete `BaseCapabilities` subclass in an external plugin, but the
current runtime does not yet provide capability-provider selection:

- built-in modules are registered first and duplicate module names are startup errors;
- every registered method must already be in the fixed `CAPABILITY_METHODS` tuple; and
- the built-in `search`, `content`, `session`, and `llm` families currently occupy every public
  method family.

Consequently, external packages cannot currently replace a built-in capability or introduce a new
public capability without a core release. A complete third-party capability surface still needs a
provider/selection layer, namespaced plugin configuration, and an explicit contract-extension
policy. The current discovery mechanism keeps core capabilities modular and prepares that future
boundary without silently weakening the wire contract.

`BrokerBuilder(enabled_capabilities=...)` may select a subset of already registered built-in
modules. The `session` methods remain required. Selection changes the effective manifest, not the
wire schema, so it does not by itself require a contract revision.

## When to change `CAPABILITY_CONTRACT`

`BrokerPlugin.api_version` and `CAPABILITY_CONTRACT` protect different boundaries:

| Change | Plugin API version | Capability contract |
| --- | --- | --- |
| Refactor discovery, assembly, constructors, or module layout | unchanged | unchanged |
| Add or replace a backend provider within an existing role | unchanged | unchanged |
| Enable or disable an existing capability subset through the manifest | unchanged | unchanged |
| Incompatibly change the Python plugin registration/build API | bump | unchanged |
| Add, remove, or rename a public RPC method | unchanged unless plugin API also changes | bump |
| Incompatibly change request/result/error or manifest wire semantics | unchanged unless plugin API also changes | bump |

For a capability-contract change, update the host `CAPABILITY_METHODS` and
`CAPABILITY_CONTRACT`, update the matching SDK transport and public method implementation, and add
contract tests before releasing both sides together. The SDK requires an exact contract match.

## Embedded composition

Applications embedding OpenSAC may supply plugins directly and optionally disable installed entry
point discovery:

```python
from opensac.api import create_app
from opensac.broker import BrokerBuilder, BrokerPlugin

from example_opensac import backends


plugin = BrokerPlugin.from_modules(backends)
app = create_app(
    settings,
    broker_builder=BrokerBuilder(
        plugins=(plugin,),
        discover_installed_plugins=False,
    ),
)
```

Built-in registrations are always included. Programmatic and installed plugins add registrations;
they do not override an existing provider or capability with the same role/name key.

## Validation checklist

Before publishing a plugin or core capability change, verify:

- entry points load without importing unrelated modules;
- provider and capability names are unique;
- selected backend factories construct successfully;
- search/document logical route names match;
- backend protocol fields and methods pass startup validation;
- request models reject unknown or incorrectly typed fields;
- capability methods match their module family and the core contract; and
- shutdown closes any adapter-owned clients.

For changes inside this repository, run the focused checks first:

```bash
uv run pytest tests/test_backend_catalog.py tests/test_broker.py tests/test_api.py
uv run ruff check .
uv run ruff format --check .
```
