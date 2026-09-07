# Implemented architecture

The development checkout is OpenSaC-next. The intended public identity remains liuqi6777/OpenSaC;
this implementation is not published yet.

## Local Python library

`from opensac import sdk` exposes research capabilities in the caller's Python environment.
`Client(settings=...)` provides explicit configuration and lifecycle ownership.

```text
Python caller → SDK → sync bridge → Runtime → Provider → backend service
```

There is no OpenSaC HTTP server, remote client mode, server authentication or Docker deployment.
Providers may call external HTTP services; local execution does not mean offline execution.
Python code, variables and files belong to the caller. There are no execution sessions, document
registries, remote code execution, MCP or agent-run interfaces.

## Modules and lifecycle

- `client.py`: synchronous SDK and lazy singleton.
- `bridge.py`: one lazy AnyIO blocking portal, keeping async resources on a persistent event loop.
- `runtime.py`: ordinary async methods with argument validation, batches, deadlines and concurrency.
- `contracts.py`: shared request/result models; no HTTP envelopes or request IDs.
- `provider.py`: independent capability protocols, entry-point discovery and lazy instance ownership.
- `providers/`: implementations grouped by service/protocol, sharing HTTP helpers in `base.py`.
- `structured.py`: bounded simple Schema and JSON output checks.
- `compose.py`: local weighted RRF fusion and exact-URL deduplication.

Import starts no thread or network request. First SDK use reads provider configuration; each provider
loads on first capability use. The sync bridge works inside notebooks with an existing event loop.
Async callers can use Runtime directly and close it with `await runtime.aclose()`.
Close Client/Runtime after active calls finish. Closing the singleton allows fresh configuration on
next use; an explicit Client remains closed. Provider instances shared across capabilities close once.
Initialization failures remain failed until a fresh Runtime, avoiding repeated initialization per batch item.

SDK methods pass arguments directly to Runtime methods through the bridge. There is no operation
registry, payload-dictionary conversion or request-type dispatcher. Simple search/fetch and batch
inputs use method parameters. Rerank providers also accept ordinary arguments; Runtime checks top_n and total text length.
LLM providers accept a prompt and optional schema directly. Model, token limit and temperature
belong to Provider configuration. Result models remain typed and serializable. Input-only Annotated
types live in Runtime; model-name validation lives in configuration, and output URL validation stays
with result contracts.
Batch arguments are validated in full before execution, preserving all-or-nothing input validation.

## Provider boundaries

Search, fetch, rerank and LLM are independent protocols and Python entry-point groups. Factories
receive ProviderConfig and ProviderContext and must not perform network I/O during construction.
Install providers and configure credentials in the caller's environment. Adding a provider requires
no core registry edits. Hot replacement is not implemented.

Search returns backend-native URLs; fetch accepts those URLs without registration. Shared HTTP(S)
format validation accepts public and local service URLs. Target acceptance, resolution and access
policy belong to the backend. Jina Reader decides whether it can process a target; Serper/Jina
adapters do not impose a public-address filter.

Runtime validates provider results and enforces search limits, fetch URL preservation and rerank
indices. SDK rerank maps indices back to original caller objects; ranking is represented by list order.
Only selected text is sent to the rerank provider. No document identifier or rank field is introduced.

## Bounds and failures

Operation deadlines include waiting for a concurrency slot. Built-in HTTP providers bound response
bytes and sanitize upstream errors; custom providers must bound their own I/O. Batch results align
with input order, including duplicates. Expected failures appear per item; invalid batch parameters
fail the whole call. Cancellation propagates.

Schema preparation and output validation run synchronously in process with size, depth and node
limits. They are bounded work, not independently interruptible CPU tasks. References, regex and
composition are unsupported. Non-finite output numbers are rejected, including in extra fields.
See [model contracts](models.md) and [SDK contracts](contracts.md).

No shared cache, automatic retry, cumulative usage accounting or RL environment management is
implemented. Agent instructions remain a documentation draft for a future standalone skill.

## Provenance

Legacy baseline: liuqi6777/OpenSaC commit f9b3e41995a236ee49a6d853f3f3a08d370a2555 (MIT).
Serper/Jina request shapes were adapted from existing implementations; LICENSE is preserved.
Execution, session and document-admission code was not migrated. Composition helpers were
implemented independently; no reference SDK source was copied.
