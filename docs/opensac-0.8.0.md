# OpenSAC v0.8.0

OpenSAC v0.8.0 replaces backend-specific operation dispatch with a capability-oriented execution
architecture. Capabilities now compose reusable services, services bind deployment policy to one
backend role, and backend adapters contain only provider I/O plus traits that represent real
behavioral differences. This is an intentional breaking migration with no compatibility layer for
the superseded YAML and batch-result shapes.

## Capability, service, and backend boundaries

Search, Content, and LLM capabilities retain task-level validation, budgets, orchestration, and
fallback behavior. Provider execution now sits behind four peer services:

- `SearchService` binds a search backend and search policy;
- `DocumentService` binds a document backend and document policy;
- `RerankService` binds a generic text reranker and rerank policy;
- `LLMService` binds an LLM backend and LLM policy.

`ProviderOperation` and operation-string policy dispatch have been removed. Each service owns one
`ProviderRuntime`, while provider identity continues to govern shared endpoint and credential
capacity. Cache keys, in-flight coalescing, retries, deadlines, cancellation, and attempt tracing
therefore remain centralized without requiring a backend to know which capability invoked it.

Backend protocols are role-specific and explicitly expose only behavior the broker cannot infer.
Search adapters declare domain-filter and depth support, with transport batching detected through
the optional `BatchSearchBackend` protocol. Document adapters declare `source_kind` and ordered
fetch candidates, which lets Content enforce opaque-source admission or public direct-URL admission
without testing provider names. LLM is now a peer backend rather than a special runtime path.

The composition root still validates one supported search/document source-family pair: `local` +
`local`, or `serper` + `jina`. Rerank and LLM backends can be selected independently of that pair.

## Shared reranking

Reranking is now a generic text service reusable by both Search results and Content passages.
Lexical BM25 has moved into a first-class `LexicalReranker` backend beside the Jina implementation;
capabilities no longer carry a separate lexical scoring implementation. The selected reranker is
always enabled and is reported to sandbox sessions as `lexical` or `jina`.

The default remains deterministic in-process BM25. Jina requires an explicit model and continues
to read its credential only from `OPENSAC_JINA_API_KEY`.

## Contracts and model ownership

The former monolithic `_contracts.py` has been removed. Provider-boundary models now live with
their backend role, capability result models live with their owning capability, shared sanitized
failure fields live in `broker.failures`, and serializable execution records live in `tracing.py`.

Backend outputs use strict, frozen Pydantic models and are validated at the service boundary.
`DocumentHandle` is now one of those models. Document candidate lists must be non-empty and preserve
the broker-authorized source; malformed provider values fail as `provider_invalid_response` before
a transport attempt is recorded.

## Flat failures and batch reports

Successful values no longer contain an optional nested `failure`. Request-wide failures raise
`BrokerError`; failure-aware batch methods return sibling `results`, `failures`, and `input_count`
collections, with `input_index` preserving the relationship to the request. Content grep similarly
keeps successful scans in `source_results` and fetch failures in a separate `failures` list.

Provider diagnostics now use the service-level `component` field (`search`, `document`, `rerank`, or
`llm`) instead of `operation`. Provider attempt and coalescing correlation identifiers are named
`request_id` rather than `operation_id`. The bundled SDK, type stubs, examples, agent adapters, and
Search-as-Code skill references have all migrated to the new report shape.

## Configuration migration

Backend choice and connection details now live under `backends`; capability admission and task
limits live under `capabilities`; provider execution overrides use fixed service slots under
`providers.services`:

```yaml
backends:
  search:
    provider: serper
  document:
    provider: jina
  rerank:
    provider: lexical
  llm:
    provider: none
    model: ""
    base_url: null

capabilities:
  search:
    max_queries_per_request: 64
    max_query_chars: 4096
    max_top_k: 600
  content:
    max_sources_per_request: 256
    url_admission: searched_or_public_web
    batch_deadline_seconds: 60

providers:
  services:
    search:
      concurrency: 6
    document:
      attempt_timeout_seconds: 30
    rerank:
      concurrency: 2
```

The rerank providers are `lexical` and `jina`; Jina requires `backends.rerank.model`, while lexical
rejects it. The optional LLM providers are `none` and `openai_compatible`; an enabled backend
requires a model, and service-level LLM policy overrides are invalid while it is disabled.

Legacy top-level backend/capability fields, `passage_ranker`, `passage_reranker_model`, and all
`operation_*` provider policy maps are rejected rather than translated. YAML remains strict about
unknown and duplicate keys, and provider credentials remain environment-only secrets.

## Observability and compatibility

Attempt accounting is now attributed to the invoking capability through
`provider_attempts_by_capability`, so a shared rerank service remains distinguishable when called
from Search or Content. Health and cache manifests expose service names instead of operation keys,
and traces retain route identity, provider identity, request fingerprints, attempts, retries,
deduplication, coalescing, cache events, and bounded diagnostics.

The capability contract increases from `11` to `12` for the SDK result and diagnostic changes. The
sandbox contract remains `13`; deploy matching v0.8.0 service and sandbox images. Persistent
interpreter behavior and its opt-in default are unchanged.
