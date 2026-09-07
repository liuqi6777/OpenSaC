# Rerank and LLM capabilities

Both capabilities run through the local Runtime. They are independent
providers in `opensac.rerank` and `opensac.llm`. Neither loads search/fetch nor owns a session.
Configure their endpoints and credentials in the caller’s Python environment.

## Configuration

```bash
export OPENSAC_RERANK_PROVIDER=http
export OPENSAC_RERANK_BASE_URL=http://localhost:9002/v1
export OPENSAC_RERANK_MODEL=your-rerank-model
export OPENSAC_RERANK_API_KEY=your-provider-key

export OPENSAC_LLM_PROVIDER=openai
export OPENSAC_LLM_BASE_URL=http://localhost:9003/v1
export OPENSAC_LLM_MODEL=your-model
export OPENSAC_LLM_API_KEY=your-provider-key
```

Endpoint and model are required when the capability is used; no external model or paid endpoint is
selected implicitly. Local endpoints may omit API keys. Use a fresh Client after changing settings.
The host configures model selection for both LLM and rerank; individual SDK calls cannot override it.
Set `OPENSAC_LLM_MAX_TOKENS` (default 1024, range 1–65536) and optionally
`OPENSAC_LLM_TEMPERATURE` (range 0–2). These apply to single and batch generation and extraction.
`Settings` exposes corresponding `rerank_*` and `llm_*` Python fields. Optional `*_OPTIONS` is a JSON
object of backend-specific request fields. Managed LLM fields cannot be overridden through options.

## Rerank

```python
from opensac import sdk

hits = sdk.search("efficient retrieval")
best_hits = sdk.rerank("efficient retrieval", hits)
```

Returns a list of the selected original objects in relevance order. SearchHit uses title + snippet,
Document uses text, and strings pass through. Other object types require a `text` callback:
`sdk.rerank(query, items, text=lambda item: item["body"])`. Objects and their metadata remain
unchanged; no scores or rank fields are attached. Only selected text crosses the provider boundary,
Empty input returns [] without a provider request.

`top_n` must not exceed the nonempty input length; omit it to rank all inputs. Up to 100 texts,
200,000 characters per text and 500,000 total characters are allowed. No source text is silently
truncated by OpenSaC. The backend may have its own token truncation policy.

Provider interfaces return zero-based input `index` and finite `relevance_score`.
Runtime validates and orders these results; the SDK maps them back to the original objects.
The index is only a position in this call's input, never a document identifier or lookup handle.
Missing, duplicate or out-of-range positions are rejected.

The built-in `http` provider posts `model`, `query`, `documents` and `top_n` to `<base_url>/rerank`,
expecting `{"results": [{"index": 0, "relevance_score": 0.9}]}`. Its wire shape follows the
[Cohere rerank API](https://docs.cohere.com/reference/rerank); use a compatible service or another provider.

## Completion and extraction

```python
from opensac import sdk

answer = sdk.llm.complete("Explain reciprocal rank fusion.")
print(answer.text)

schema = {
    "type": "object",
    "properties": {"title": {"type": "string"}},
    "required": ["title"],
    "additionalProperties": False,
}
result = sdk.llm.extract("Title: Efficient Retrieval", schema)
print(result.data["title"])
```

`complete` returns `Completion(text, model, usage)`; `extract` returns
`Extraction(data, model, usage)`. Optional `usage` contains provider-reported input/output/total token
counts; missing counts stay `None`, not zero. This is per-response metadata, not cost calculation,
retry accounting or a cumulative budget. Save results using `model_dump_json()`.

Completion accepts only the prompt. Extraction accepts text, schema and an optional `instruction`
string describing the task. Model and generation parameters belong to Provider configuration. `complete_many(prompts, ...)` and `extract_many(texts, schema, ...)`
return up to 32 input-aligned BatchItems, each containing data or an expected capability error.
Invalid batch parameters or schemas fail the whole batch; a model failure affects its own item.

The `openai` provider implements non-streaming text
[Chat Completions](https://developers.openai.com/api/reference/python/resources/chat/subresources/completions/methods/create),
posting to `<base_url>/chat/completions`. It sends `max_completion_tokens`, omits temperature unless
requested, and requests strict `response_format: json_schema` for extraction. A compatible backend
must support these fields and the schema subset it advertises; unsupported features fail explicitly,
without fallback requests or retries. Tool calls, multimodal input and streaming are not implemented.

The shared core accepts a small JSON Schema subset, checked in process without subprocesses:

| Rule | Supported behavior |
| --- | --- |
| `type` | Required: object, array, string, integer, number, boolean or null; a list allows simple unions |
| `properties`, `required` | Nested fields; required names must be declared in properties |
| `additionalProperties` | Boolean only, defaults to true |
| `items` | Required for arrays; one schema for all items |
| `enum` | 1–100 scalar values matching the declared types |
| `title`, `description` | Optional text annotations |

Every other keyword is rejected as `invalid_schema` before generation. This includes `$ref`,
`$defs`, `$id`, `pattern`, `anyOf`, `oneOf`, `allOf` and `const`. Schemas generated by other
libraries must fit this subset; arbitrary Pydantic schemas are not automatically supported.

Schemas allow at most 50,000 serialized characters, 2,000 JSON value nodes and 8 nested schema
levels below the root (raw JSON depth is limited to 24). Outputs allow at most 20,000 value nodes,
JSON depth 16 and the configured `max_response_bytes` UTF-8 byte limit. Root depth is zero.
Validation does not coerce types, strip Markdown fences or accept NaN/Infinity, including
exponent overflow such as `1e400` in additional fields. Integer fields require JSON integer values;
booleans and floating-point values do not count as integers.

Provider-specific schema restrictions still apply. Plain completion prompts allow 204,096 characters;
extraction text and each batch prompt allow 200,000.

Refusals raise `model_refusal`; token-limit truncation raises `generation_incomplete`;
invalid extraction output raises `structured_output_invalid`. Upstream bodies and source text are
not included in public errors. The existing timeout, response-size and concurrency controls apply.

## Custom providers

```toml
[project.entry-points."opensac.rerank"]
my-ranker = "my_package:Reranker"

[project.entry-points."opensac.llm"]
my-model = "my_package:Model"
```

Each factory takes `ProviderConfig` and `ProviderContext`. Rerank providers implement
`async rerank(query, documents, *, top_n=None) -> list[RerankResult]`; LLM providers implement
`async complete(prompt, *, schema=None) -> Completion`. Both implement `async aclose()`.
LLM providers receive the requested JSON schema in the `schema` keyword argument; extraction orchestration
and final schema validation belong to Runtime, so an LLM provider needs only one generation method.
Import results from `opensac.contracts`, and provider protocols from `opensac.provider`.

Tests and wheel integration use controlled HTTP services. No paid model call has been verified.

Provider calls retain their timeout and concurrency controls. Local validation uses bounded
synchronous work rather than a separately interruptible process. Invalid output is reported as
`structured_output_invalid`, never silently serialized to null.
