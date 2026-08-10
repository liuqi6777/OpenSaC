# OpenSAC

OpenSAC is an open reference implementation of the Search as Code architecture. A control
model generates Python, a locked-down Docker sandbox executes it, and an embedded SDK exposes
search primitives through a host-side capability broker.

See [the design goals and capability roadmap](docs/design.md) for the intended system
properties, current implementation status, primitive-selection criteria, and next milestones.

## Architecture

```text
OpenAI-compatible model -> generated Python -> Docker sandbox
                                                |
                                         opensac_sdk
                                                |
                                         Unix socket RPC
                                                |
                                  host capability broker
                                     /                 \
                            local search HTTP       Serper API
```

The sandbox has no network, credentials, Docker socket, or host filesystem access beyond its
session workspace. Search references are opaque and scoped to a broker session. The broker
enforces backend permissions, concurrency, search-call limits, and LLM-call limits.

## Requirements

- Python 3.12
- `uv`
- Docker for real sandbox execution
- An OpenAI-compatible chat-completions endpoint
- At least one configured search backend

The current development machine does not contain a Docker runtime. Command generation and
security flags are tested, but the image must be built and integration-tested on a Docker host.

## Setup

```bash
uv sync --extra dev
cp .env.example .env
uv run opensac build-sandbox
uv run opensac serve
```

Rebuild the sandbox image after upgrading this checkout. The preflight probe checks its
contract version so a stale tag cannot silently miss bundled SDK or entrypoint changes.

Configure `OPENSAC_MODEL_API_KEY`, `OPENSAC_MODEL_NAME`, and optionally
`OPENSAC_MODEL_BASE_URL`. Set `OPENSAC_API_KEY` outside local development.

For local retrieval, run the compatible DeepResearch endpoint at
`OPENSAC_LOCAL_SEARCH_BASE_URL`. It must expose `POST /search`, `POST /search_many`,
and `POST /get_document`.

For web retrieval, set a [Serper](https://serper.dev) API key. No extra dependency is needed --
the backend talks to Serper's search and scrape endpoints over plain HTTP:

```bash
export OPENSAC_SERPER_API_KEY=...
```

## Submit A Task

```bash
uv run opensac run "Compare official vector search capabilities of three databases"
```

Or use the HTTP API:

```bash
curl -X POST http://127.0.0.1:8000/v1/sessions \
  -H "Authorization: Bearer $OPENSAC_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"backends":["web"],"limits":{"max_turns":8}}'

curl -X POST http://127.0.0.1:8000/v1/sessions/SESSION_ID/runs \
  -H "Authorization: Bearer $OPENSAC_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"input":"Research the task and cite primary sources"}'
```

The API exposes:

```text
POST   /v1/sessions
GET    /v1/sessions/{session_id}
DELETE /v1/sessions/{session_id}
POST   /v1/sessions/{session_id}/runs
POST   /v1/sessions/{session_id}/exec
GET    /v1/runs/{run_id}
GET    /v1/runs/{run_id}/events
POST   /v1/runs/{run_id}/cancel
GET    /v1/runs/{run_id}/artifacts/{path}
```

## Bring Your Own Control Model

`runs` delegates the whole task to OpenSAC's control model. `exec` inverts that: an
external agent harness generates the program and OpenSAC contributes only the sandbox,
the SDK, and the capability broker. Session state persists across `exec` calls -- the
workspace filesystem and the search reference table -- so a program can serialize
intermediate results in one turn and resolve their refs several turns later.

```python
from opensac import OpenSAC

with OpenSAC(api_key="...") as client:
    session = client.create_session(backends=["local"])
    result = client.exec_code(
        session["id"],
        "from opensac_sdk import sdk\n...",
        exec_id="rollout-17:turn-3",
        include_trace=True,
    )
    print(result["stdout"], result["output"], result["usage"])
    client.delete_session(session["id"])
```

A program rejected by the sandbox code validator comes back as a normal result with
`succeeded: false` and a populated `error`, so the calling harness can feed the reason
straight back to its model. `OPENSAC_SANDBOX_MAX_CONCURRENCY` caps how many programs execute
at once across all sessions; warm-container lifetime is bounded separately by
session deletion/TTL and `OPENSAC_SANDBOX_WARM_IDLE_SECONDS`.
Warm containers are owner-labeled by broker socket; a restarted OpenSAC process removes
containers orphaned by a prior crash without touching other instances on the host.

`exec_id` is an idempotency key. A retry with the same payload returns the durable original
response; reusing it for different code returns 409. The session manifest advertises
`features: ["idempotent_exec"]`, allowing older clients and servers to negotiate retries safely.

## Throughput tuning

Cold mode starts one container per program. Warm mode keeps the hardened namespace and mounts
per session while each program still runs in a fresh `python -I` child process:

```bash
OPENSAC_SANDBOX_MODE=warm \
OPENSAC_SANDBOX_WARM_IDLE_SECONDS=300 \
OPENSAC_SESSION_TTL_SECONDS=3600 \
uv run opensac serve
```

Every `/exec` response includes phase timings for the session queue, global sandbox queue,
validation/setup, startup, program execution, and post-processing. `/healthz` exposes active
and waiting sandbox work. Measure cold and warm on the target Docker host with:

```bash
uv run python scripts/benchmark_exec.py \
  --concurrency 1,4,8,16 --requests 64 --output benchmark.json
```

For a retrieval workload, pass `--code-file program.py`; capability latency is aggregated from
the returned trace. Warmup always runs `pass`, so it warms the executor without populating the
measured session's retrieval cache, references, or workspace. The report keeps HTTP/transport,
program, warmup, and cleanup failures instead of stopping at the first saturated request, and
records the server health snapshot plus the measured code hash. The local retriever uses
`/search_many` and a bounded GPU microbatch rather than serializing one model forward per query.
The broker rejects a capability call before fan-out when it exceeds
`OPENSAC_SEARCH_MAX_QUERIES_PER_REQUEST` (default 64), `OPENSAC_SEARCH_MAX_QUERY_CHARS`
(default 4096), or `OPENSAC_SEARCH_MAX_TOP_K` (default 600, measured as `offset + limit`).
These per-request safety limits are independent of measured rollout-level search usage.

See `examples/research_pipeline.py` for representative model-generated code.

The external Python client supports both synchronous and asynchronous applications:

```python
from opensac import OpenSAC

with OpenSAC(api_key="...") as client:
    session = client.create_session(backends=["local"])
    result = client.create_and_wait(session["id"], "Research the task")
```

```python
from opensac import AsyncOpenSAC

async with AsyncOpenSAC(api_key="...") as client:
    session = await client.create_session(backends=["web"])
    result = await client.create_and_wait(session["id"], "Research the task")
```

## Tests

```bash
uv run ruff check .
uv run pytest
```
