# OpenSAC

OpenSAC is an open reference implementation of the Search as Code architecture. An external
agent generates Python, a locked-down Docker sandbox executes it, and an embedded SDK exposes
search primitives through a host-side capability broker.

See [the design goals and capability roadmap](docs/design.md) for the intended system
properties and primitive-selection criteria. See the
[OpenSAC 0.4 release notes](docs/opensac-0.4.md) for the compact agent contract and migration
guide. The underlying reliability design is recorded in the
[OpenSAC 0.3 high-fan-out reliability plan](docs/opensac-0.3-plan.md).

## Architecture

```text
external agent harness -> generated Python -> OpenSAC API -> Docker sandbox
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
- At least one configured search backend
- Optionally, an OpenAI-compatible chat-completions endpoint for `sdk.llm.*`

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

Set `OPENSAC_API_KEY` outside local development. To expose the optional pipeline-LLM
capabilities to sandbox programs, configure `OPENSAC_MODEL_API_KEY`,
`OPENSAC_MODEL_NAME`, and optionally `OPENSAC_MODEL_BASE_URL`.

Choose one search backend for the whole OpenSAC service. The default is `local`:

```bash
export OPENSAC_SEARCH_BACKEND=local  # or web
```

For local retrieval, run the compatible DeepResearch endpoint at
`OPENSAC_LOCAL_SEARCH_BASE_URL`. It must expose `POST /search`, `POST /search_many`,
and `POST /get_document`. Search hits provide a server-shaped `snippet`; modes that extract
document metadata also provide `title` and `date`. OpenSAC consumes those fields without
extracting or truncating the snippet again. Configure `full`, `compact`, or `query_aware`
result mode on the DeepResearch search server, not in OpenSAC. Programs can fetch the complete
document separately through the content SDK, which uses `/get_document`.

For web retrieval, select `web` and set a [Serper](https://serper.dev) API key. No extra dependency is needed --
the backend uses Serper for search and Jina Reader for fetching result content:

```bash
export OPENSAC_SEARCH_BACKEND=web
export OPENSAC_SERPER_API_KEY=...
export OPENSAC_JINA_API_KEY=...
```

## Execute A Program

The external agent owns the control loop. Create one session per rollout, then submit each
agent-generated Python program through `/exec`:

```bash
curl -X POST http://127.0.0.1:8000/v1/sessions \
  -H "Authorization: Bearer $OPENSAC_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{}'

curl -X POST http://127.0.0.1:8000/v1/sessions/SESSION_ID/exec \
  -H "Authorization: Bearer $OPENSAC_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"code":"from opensac_sdk import sdk\nhits = sdk.search(\"vector databases\")\nsdk.output.submit({\"hits\": [h.model_dump() for h in hits]})"}'
```

The API exposes:

```text
POST   /v1/sessions
GET    /v1/sessions/{session_id}
DELETE /v1/sessions/{session_id}
POST   /v1/sessions/{session_id}/heartbeat
POST   /v1/sessions/{session_id}/abort
GET    /v1/sessions/{session_id}/workspace
POST   /v1/sessions/{session_id}/exec
POST   /v1/admin/drain
```

## OpenSAC 0.4 agent surface

Multi-query fusion is a local SDK operation: it preserves every query/rank source and does not
make a broker call or consume capability budget.

```python
batches = sdk.search.many(["vector database history", "ANN index origins"])
fusion = sdk.search.fuse_rrf(batches, weights=[1.0, 0.8], k=60, limit=20)
refs = [candidate.ref for candidate in fusion.candidates[:5]]
```

Structured extraction validates every model output against a restricted JSON Schema Draft
2020-12 contract. Results stay aligned with the input; malformed rows carry a typed error and
do not cancel successful siblings. Repair is explicit and limited to one attempt.

```python
rows = sdk.llm.extract_many(
    [{"title": candidate.title, "snippet": candidate.snippet}
     for candidate in fusion.candidates],
    instruction="Decide whether this result contains a dated historical claim.",
    schema={
        "type": "object",
        "properties": {
            "relevant": {"type": "boolean"},
            "year": {"type": ["integer", "null"]},
        },
        "required": ["relevant", "year"],
        "additionalProperties": False,
    },
    repair_attempts=1,
)
valid = [row.data for row in rows if row.data is not None]
```

Search and content batch rows expose one typed `failure` field. The 0.4 contract removes the
duplicate `SearchBatch.error` and `ContentSnippet.metadata["fetch_error"]` mirrors. Empty search
hits are a successful result, not a failure.

```python
batches = sdk.search.many(["query one", "query two"])
for batch in batches:
    if batch.failure is not None:
        print(
            batch.query,
            batch.failure.code,
            batch.failure.retryable,
            batch.failure.attempts,
        )
        continue
    print(batch.query, len(batch.hits))
```

Legacy `content.grep` still returns only successful matches. Use `grep_report` when coverage
must distinguish a genuine zero-match document from a fetch failure, or when duplicate input
refs must remain distinguishable.

```python
report = sdk.content.grep_report(refs, r"release(?:d)? in 19\d{2}", context=2)
for failure in report.failures:
    print(failure.input_index, failure.ref, failure.failure.code)
for match in report.matches:
    print(match.input_index, match.ref, match.line, match.text)
```

`content.read`, `content.snippets`, and `content.grep` attach a broker-issued locator to each
eligible passage while the session evidence registry has capacity. Passing that locator makes
the final citation point at the selected passage; passing only a ref keeps the legacy
search-preview citation.

```python
passage = sdk.content.read([refs[0]], offset=1, limit=20)[0]
if passage.locator is not None:
    sdk.output.submit(
        {"answer": "..."},
        citations=[{"ref": passage.ref, "locator": passage.locator}],
    )
elif passage.locator_error is not None:
    print("passage is usable but not citable as selected evidence:", passage.locator_error.code)
```

The bounded registry defaults to 4,096 locators and 32 MiB of unique UTF-8 passage bytes. Once
full, content still returns the passage with `locator=None` and
`locator_error.code == "evidence_capacity_exhausted"`; existing locators stay valid. Do not
pass explicit `locator: null` or manufacture a locator. A ref-only citation is an intentional
search-preview citation, never an implicit fallback for selected evidence.

The session manifest reports capability contract `3`, sandbox contract `5`, feature flags,
content/evidence limits, and the effective policies for the selected backend. Sessions without a configured pipeline model do not advertise
`llm.*` methods. Retry and rate limits are deployment policy rather than per-call SDK options;
the default retry profile remains `none`. Intra-call dedupe preserves logical rows and usage.
Session-local in-flight coalescing remains optional and disabled by default.

Sandbox programs see a compact `sdk.session.usage()` view containing strategy counts,
`documents_seen`, `budget_remaining`, and terminal state. Provider attempts, retries, queueing,
cache effects, coalescing, and evidence-registry measurements remain available to the external
harness through session usage and capability trace rather than entering normal agent control
flow.

## External Control Model

OpenSAC deliberately does not run an agent loop. The external agent harness generates each
program; OpenSAC contributes only the sandbox, SDK, and capability broker. Session state
persists across `exec` calls -- the workspace filesystem and search reference table -- so a
program can serialize intermediate results in one turn and resolve their refs several turns
later.

```python
from opensac import OpenSAC

with OpenSAC(api_key="...") as client:
    session = client.create_session(
        request_id="rollout-17:attempt-1",
        lease_seconds=600,
        budget={
            "max_exec_calls": 16,
            "max_search_queries": 128,
            "max_content_fetches": 256,
            "max_sandbox_seconds": 300,
        },
    )
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
an explicit feature list, allowing older clients and servers to negotiate retries safely. RL
workers advertise worker affinity, idempotent session creation, leases, resource budgets, and
abort in addition to idempotent execution.

## RL environment workers

OpenSAC is intentionally a node-local, single-process environment worker. An external trainer
or rollout scheduler stores `{endpoint, worker_id, worker_epoch, session_id}` and keeps every
request for that session on the same endpoint. A worker restart changes `worker_epoch`; old
sessions return `410 worker_restarted` because their in-memory reference table cannot be
reconstructed safely. The scheduler starts that rollout attempt again on a healthy worker.

`request_id` makes session creation safe to retry on the same worker. A lease is renewed by
successful session operations or explicitly through `heartbeat`; `abort` cancels admitted work
and removes its container immediately. Session `DELETE` is graceful: it stops new admission and
waits for accepted execution before teardown. `drain` rejects new sessions while existing
sessions can finish. Capacity and process RSS/FD snapshots are reported by `/healthz` for
external placement.

Resource ceilings are opt-in and enforced per session. Discrete calls are reserved before the
backend side effect, failed calls remain charged, and an idempotent `/exec` replay is not charged
twice. A result that reaches its final allowance returns `session_state="exhausted"`; subsequent
new executions receive the machine-readable `409 budget_exhausted` response. Workspace bytes are
checked at admission and audited after each execution; because the workspace is a portable Docker
bind mount, deployments that must prevent a transient single-exec disk burst also need a
filesystem/project quota on each worker data directory.

See [RL worker deployment](docs/rl-environment-workers.md) for multi-instance configuration and
the scheduler failure contract.

## Throughput tuning

Cold mode starts one container per program. Warm mode keeps the hardened namespace and mounts
per session while each program still runs in a fresh `python -I` child process:

```bash
OPENSAC_SANDBOX_MODE=warm \
OPENSAC_SANDBOX_WARM_IDLE_SECONDS=300 \
OPENSAC_SANDBOX_MAX_WARM_CONTAINERS=32 \
OPENSAC_MAX_ACTIVE_SESSIONS=128 \
OPENSAC_SESSION_TTL_SECONDS=3600 \
uv run opensac serve
```

Every `/exec` response includes phase timings for the session queue, global sandbox queue,
validation/setup, startup, program execution, and post-processing. `/healthz` exposes active
and waiting sandbox work. Measure cold and warm on the target Docker host with:

```bash
uv run python scripts/benchmark_exec.py \
  --concurrency 16,32,64,128 --duration-seconds 3600 --output soak.json
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
    session = client.create_session()
    result = client.exec_code(session["id"], "print('ready')\n")
```

```python
from opensac import AsyncOpenSAC

async with AsyncOpenSAC(api_key="...") as client:
    session = await client.create_session()
    result = await client.exec_code(session["id"], "print('ready')\n")
```

## Tests

```bash
uv run ruff check .
uv run pytest
```
