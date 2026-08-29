# OpenSAC v0.8.2

OpenSAC v0.8.2 makes `sdk.search.many` a client-side composition over unary search. The SDK issues
bounded concurrent `search.query` calls; the broker no longer exposes or implements
`search.query_many`. This keeps the broker boundary unary while preserving its per-request budget,
provider concurrency, rate-limit, retry, deadline, cache/coalescing, trace, and source-admission
policies.

There is no compatibility handler or mode switch. Capability contract 14 rejects mixed 0.8.1 and
0.8.2 SDK/broker deployments. Sandbox contract 14 is unchanged.

## Search many

`sdk.search.many` keeps its public signature. It checks the session capability manifest, then uses a
bounded `ThreadPoolExecutor` to call `search.query` once per input. Outcomes remain aligned to input
order even when calls complete out of order, and duplicate queries are not deduplicated by the SDK.
The helper waits for every worker to finish before returning.

The private many runner is resource-neutral. It records input identity, normalizes `BrokerError`
details, counts successes and failures, and applies the common all-system-failure promotion rule.
Search retains only its admission, outcome payload, and query-specific diagnostics, so a future
content many helper can reuse the same execution and failure semantics.

The concurrency argument is SDK helper admission, not the provider semaphore. The broker still
governs each unary provider call, while the configured search backend owns its actual retrieval
implementation and any internal batching.

The broker-facing `BatchSearchBackend` fast path and `LocalSearchBackend.search_many` adapter method
were removed because the broker no longer consumes provider batches. The standalone local search
service may continue to batch internally; this is outside the broker capability contract.

## Outcomes and errors

Every row now has one schema. For example:

```python
success = {
    "query": "...",
    "status": "success",
    "hits": [...],
    "error": None,
}

failure = {
    "query": "...",
    "status": "failure",
    "hits": [],
    "error": {
        "code": "...",
        "message": "...",
        "retryable": False,
        "attempts": 1,
        "provider_status": None,
        "retry_after_seconds": None,
        "provider": "...",
        "component": "search",
        "scope": "provider",
    },
}
```

Provider, quota, and deadline failures remain per-item outcomes. If every item fails with a
transport, protocol, contract, or permission error, `many` raises one representative top-level
`BrokerError`. Unexpected non-`BrokerError` exceptions still propagate.

## Scope

No `content.fetch_many` or `llm.extract_many` API was added. Callers continue to loop over those
unary methods and catch `BrokerError` per item. There is no SDK environment variable selecting a
broker/client implementation, and no broker batch compatibility shim.

Client-side fan-out changes batch-level budget atomicity, trace shape, native provider batch use,
and source-registration order compared with 0.8.1. Each unary call is independently admitted and
traced. The session manifest's `mechanisms.batching` and
`search.limits.max_queries_per_request` fields now act only as `search.many` helper admission
metadata; they do not describe a broker batch RPC.

## Deployment

Deploy matching `0.8.2` service and sandbox images. Programs generated for 0.8.1 must be regenerated
or migrated because the capability contract and failed-outcome schema changed. Tagging and
publishing remain separate release steps described in `docs/releasing.md`.
