# OpenSAC

OpenSAC is an open reference implementation of the Search as Code architecture. A control
model generates Python, a locked-down Docker sandbox executes it, and an embedded SDK exposes
search primitives through a host-side capability broker.

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

Configure `OPENSAC_MODEL_API_KEY`, `OPENSAC_MODEL_NAME`, and optionally
`OPENSAC_MODEL_BASE_URL`. Set `OPENSAC_API_KEY` outside local development.

For local retrieval, run the compatible DeepResearch endpoint at
`OPENSAC_LOCAL_SEARCH_BASE_URL`. It must expose `POST /search` and `POST /get_document`.

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
    session = client.create_session(backends=["local"], limits={"max_search_calls": 2000})
    result = client.exec_code(session["id"], "from opensac_sdk import sdk\n...")
    print(result["stdout"], result["output"], result["usage"])
    client.delete_session(session["id"])
```

A program rejected by the sandbox code validator comes back as a normal result with
`succeeded: false` and a populated `error`, so the calling harness can feed the reason
straight back to its model. `OPENSAC_SANDBOX_MAX_CONCURRENCY` caps how many containers
run at once across all sessions.

See `examples/research_pipeline.py` for representative model-generated code.

The external Python client supports both synchronous and asynchronous applications:

```python
from opensac import OpenSAC

with OpenSAC(api_key="...") as client:
    session = client.create_session(backends=["web", "local"])
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
