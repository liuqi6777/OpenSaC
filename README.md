# OpenSaC

Search, fetch, rerank and extract information from Python. OpenSaC gives agents and applications
composable research capabilities through one SDK, with independently configurable providers.

```python
from opensac import sdk

hits = sdk.search("retrieval evaluation", limit=5)
for result in sdk.content.fetch_many([hit.url for hit in hits]):
    if result.ok:
        print(result.data.url, result.data.text[:500])
    else:
        print(result.error.code)
```

OpenSaC runs in your Python process. Providers connect to search, reader and model services;
your code, variables and files stay in your environment. No OpenSaC server or session setup is needed.
Public and local sources use the same URL-based interfaces.

## Install and configure

Requires Python 3.12+. This implementation is in development and has not been published to PyPI.
Install from this checkout in a separate environment from older OpenSaC releases:

```bash
pip install .
export OPENSAC_SEARCH_PROVIDER=serper
export OPENSAC_SEARCH_API_KEY=your-search-key
export OPENSAC_FETCH_PROVIDER=jina
python examples/research.py
```

Configure only the capabilities you use. Jina's API key is optional. See [.env.example](.env.example)
for available settings; export the variables in your environment or pass explicit `Settings` to a
`Client`. The SDK does not automatically load a `.env` file.

| Capability | Built-in providers | Configuration prefix |
| --- | --- | --- |
| Search | `serper`, `http` | `OPENSAC_SEARCH_*` |
| Fetch | `jina`, `http` | `OPENSAC_FETCH_*` |
| Rerank | `http` | `OPENSAC_RERANK_*` |
| LLM | `openai` | `OPENSAC_LLM_*` |

For rerank and LLM, the host configures the endpoint and model. Generation settings also belong to
the host, so agent code supplies task content rather than model parameters:

```bash
export OPENSAC_RERANK_BASE_URL=http://localhost:9002/v1
export OPENSAC_RERANK_MODEL=your-rerank-model
export OPENSAC_LLM_BASE_URL=http://localhost:9003/v1
export OPENSAC_LLM_MODEL=your-model
export OPENSAC_LLM_MAX_TOKENS=1024
# Optional: OPENSAC_LLM_API_KEY, OPENSAC_LLM_TEMPERATURE
```

The `openai` provider supports compatible Chat Completions endpoints. HTTP search, fetch and rerank
providers connect to independent backend services; they do not require an OpenSaC server.
Provider keys stay in the caller's environment. See [model configuration](docs/models.md) and
[provider interfaces](docs/providers.md) for backend requirements.

## Research with ordinary Python objects

Combine searches, reorder the original results, fetch text and save artifacts using normal Python:

```python
from pathlib import Path
from opensac import sdk

pool = sdk.search(["retrieval evaluation", "evaluating retrieval quality"], limit=10)

if pool:
    best = sdk.rerank("retrieval evaluation", pool[:100], top_n=min(3, len(pool)))
    document = sdk.content.fetch(best[0].url)
    Path("document.json").write_text(document.model_dump_json(), encoding="utf-8")
```

`rerank` returns the selected original objects in relevance order. It accepts search hits, documents,
strings, or custom objects with a `text` callback. Only selected text is sent to the provider.
`fuse` combines ranked lists with weighted reciprocal rank fusion; `dedup` keeps the first object
per exact URL. Both run locally and accept a custom `key` callback. List order expresses ranking.

## Generate and extract

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

Extraction checks the returned JSON against a bounded, simple Schema subset: primitive types,
objects, arrays, required fields, nullable types and scalar enums. Unsupported rules fail explicitly.
Model refusals, incomplete generation and invalid output have distinct errors.
See [supported schemas and limits](docs/models.md).

## Results and errors

| Call | Result |
| --- | --- |
| `sdk.search(query_or_reformulations)` | one `list[SearchHit]`: URL, title, snippet, domain, date |
| `sdk.content.fetch(url)` | `Document`: URL and text |
| `sdk.rerank(query, items)` | `list[T]`: selected original objects |
| `sdk.llm.complete(prompt)` | `Completion`: text, model, optional token usage |
| `sdk.llm.extract(text, schema)` | `Extraction`: parsed data, model, optional token usage |

Search domains default to the URL hostname. Dates preserve provider text and are `None` when absent.
Models support attribute access, `model_dump()` and `model_dump_json()`.

Pass up to 10 reformulations of one intent as a list to `search`; it sends one provider request per
distinct string, fuses rankings locally and applies one total result limit. Use `search.many` only for
independent questions. It and the other `*_many` methods return input-aligned `BatchItem` lists. Use
`result.ok`, then `result.data` or `result.error`. Empty search results are successful. Invalid batch
arguments fail the whole call; expected provider failures affect their items.

Single-call failures raise `OpenSACError`. Import specific exceptions from `opensac.errors`, such as
`InvalidRequestError`, `ProviderRateLimitError` and `StructuredOutputError`. Error messages omit
upstream response bodies and credentials. Calls have bounded response sizes, timeouts and concurrency;
there are no automatic retries. See [SDK contracts and errors](docs/contracts.md).

## Configuration and lifecycle

Importing `sdk` starts no thread or network request. First use reads configuration; each provider
loads when its capability is first called. The synchronous SDK maintains one background event loop
and also works in notebooks. For explicit ownership:

```python
from opensac import Client
from opensac.config import Settings

with Client(settings=Settings(max_concurrency=4)) as client:
    hits = client.search("retrieval evaluation")
```

Close after active calls finish. `sdk.close()` releases resources and allows the singleton to read
fresh settings on its next use; an explicit Client remains closed. Async applications can call
`opensac.runtime.Runtime` methods directly and close with `await runtime.aclose()`.

## Host-controlled research tracing

Tracing is optional and disabled by default. It is configured by the host process, not by research
programs: generated agent code continues to call only the SDK methods above. Trace events are never
printed to stdout or stderr and are not returned by SDK calls.

```bash
export OPENSAC_TRACE_DIR=/absolute/path/outside-the-agent-workspace/opensac-traces
export OPENSAC_TRACE_RUN_ID=browsecomp-001-sac
export OPENSAC_TRACE_ACTION_ID=codex-turn-03  # optional host override
codex ...
```

Setting `OPENSAC_TRACE_DIR` enables tracing. Each process writes its own `opensac-<pid>.jsonl` file.
Every line is one completed or failed public SDK operation with its timestamp, duration, normalized
input and the exact normalized value returned to the caller. Counts, sizes and workload summaries are
derived offline rather than computed by the runtime.

When tracing is enabled and `OPENSAC_TRACE_ACTION_ID` is absent, OpenSaC generates one stable action ID
per Python process. A normal Codex shell invocation that runs one Python program can therefore be
treated as one action. A persistent Python process should set `OPENSAC_TRACE_ACTION_ID` for each host
action; dynamic action contexts are intentionally not implemented.

The recorded values are OpenSaC operation values, not raw provider transport responses. They include
search results, fetched text, prompts and model outputs, so trace files may be large and sensitive.
Credentials and provider transport details are not operation inputs and are not recorded.
The trace directory should be owned by the benchmark harness and kept outside the agent workspace.

Writing is best effort: a trace I/O failure does not affect a research call. The harness should treat
a missing trace file as a failed recording. Traces are not tamper-proof: OpenSaC runs in the same
Python environment as the caller, so this design only keeps tracing out of the agent-facing SDK and
model observations.

## Extend and develop

Providers register through Python entry points: `opensac.search`, `opensac.fetch`, `opensac.rerank`
and `opensac.llm`. Install and select a provider without editing a central registry. Each capability
has its own interface, even when several implementations live in the same package.
See [writing a provider](docs/providers.md).

```bash
uv sync
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv build
uv run python scripts/verify_wheels.py
```

The wheel check uses an isolated Python 3.12 environment, an independently installed provider and
controlled HTTP backends. It exercises all capabilities and local file output without paid API calls.

CLI, caching and RL integration are future work. Agent instructions live in the standalone
[Search as Code skill](skills/search-as-code/SKILL.md), not a package resource.
See [architecture](docs/architecture.md), [roadmap](docs/implementation-plan.md),
[examples](examples/), [contributor instructions](AGENTS.md) and
[publishing to PyPI](docs/releasing.md).

Licensed under [MIT](LICENSE). Provider adaptation provenance is recorded in
[architecture](docs/architecture.md#provenance).
