# OpenSAC v0.6.5

OpenSAC v0.6.5 makes external provider failures visible to the control agent without discarding
usable results or making correct generated Python look like a runtime failure.

## External failure warnings

Failure-aware SDK operations now preserve successful rows and emit bounded structured warnings for
every failed query, document, or extraction item. `sac_run` renders those warnings before stdout, so
an agent that only prints successful `text`, `hits`, `matches`, or `passages` still sees the missing
coverage.

This applies to:

- `sdk.search.many` and `sdk.search.fuse_rrf`;
- `sdk.content.get_many`, `read`, `read_many`, `grep`, and `passages`;
- `sdk.llm.extract_many`.

An empty result remains a valid success when no typed failure accompanies it. Partial and complete
item failure both keep the program exit code at zero when the SDK can return its documented aligned
shape. Request validation, broker transport/protocol errors, and operations without a safe result
shape still raise `BrokerError`.

The original `failure` and `failures` fields remain available for programmatic handling. Warnings
contain only bounded, secret-free provider diagnostics; they never include fetched document text,
model output, credentials, or provider response bodies.

## Passage reranker fallback

When the configured passage reranker is unavailable or returns invalid indexed scores,
`content.passages` falls back to the existing lexical BM25 scores. Returned passages identify the
effective ranker as `lexical:bm25`, and the report includes the typed reranker warning that `sac_run`
surfaces to the agent.

## Deployment compatibility

The sandbox contract is `12` and the capability contract is `11`. Deploy matching v0.6.5 service
and sandbox images. The capability bump records the new all-item failure and reranker fallback
semantics; the sandbox bump records the bundled SDK warning channel.

The REST execution response adds a backwards-compatible `warnings` list. Adapters should display it
before stdout and keep it bounded so successful evidence remains visible.
