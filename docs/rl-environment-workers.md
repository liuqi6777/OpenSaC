# RL environment worker deployment

Run one OpenSAC process per worker endpoint. Each instance needs a unique worker ID, data
directory, broker socket and API port; do not put a round-robin proxy in front of stateful
session routes.

```bash
OPENSAC_WORKER_ID=node-a-0 \
OPENSAC_API_PORT=8000 \
OPENSAC_DATA_DIR=/var/lib/opensac/worker-0 \
OPENSAC_BROKER_SOCKET=/var/lib/opensac/worker-0/broker.sock \
OPENSAC_SANDBOX_MODE=warm \
OPENSAC_BACKEND_METADATA_HASH=sha256:replace-with-index-metadata-hash \
OPENSAC_MAX_ACTIVE_SESSIONS=128 \
OPENSAC_SANDBOX_MAX_WARM_CONTAINERS=32 \
OPENSAC_SANDBOX_MAX_CONCURRENCY=16 \
uv run opensac serve

OPENSAC_WORKER_ID=node-a-1 \
OPENSAC_API_PORT=8001 \
OPENSAC_DATA_DIR=/var/lib/opensac/worker-1 \
OPENSAC_BROKER_SOCKET=/var/lib/opensac/worker-1/broker.sock \
OPENSAC_SANDBOX_MODE=warm \
OPENSAC_BACKEND_METADATA_HASH=sha256:replace-with-index-metadata-hash \
OPENSAC_MAX_ACTIVE_SESSIONS=128 \
OPENSAC_SANDBOX_MAX_WARM_CONTAINERS=32 \
OPENSAC_SANDBOX_MAX_CONCURRENCY=16 \
uv run opensac serve
```

The scheduler polls `/healthz` and chooses only workers with `accepting=true`. It stores the
endpoint and both worker identity fields returned at session creation. Retries of session
creation reuse the same `request_id` on the same endpoint. All later calls are pinned to that
endpoint.

| Response | Scheduler action |
|---|---|
| `429 capacity_exhausted` | Select another accepting worker or retry after `Retry-After`. |
| `503 worker_draining` | Select another worker. |
| `410 worker_restarted` / `session_expired` | Discard and restart the rollout attempt. |
| `409 budget_exhausted` | Record a normal terminal environment outcome. |
| `409 exec_indeterminate` | Discard and restart the rollout attempt; never replay the action under a new ID. |

Before rolling a worker, call `POST /v1/admin/drain`, wait for its active session count to reach
zero, and then stop it. A cancelled training group should call session `abort`; graceful
completion should archive workspace/trace metadata and then call DELETE.

`max_workspace_bytes` is an admission and post-execution audit boundary. Docker bind mounts do not
offer a portable per-directory byte quota, so production workers that require strict protection
against a transient write burst should place each worker data directory on a filesystem/project
quota. All other discrete, token and execution-time budgets are reserved or clamped before their
side effect.
