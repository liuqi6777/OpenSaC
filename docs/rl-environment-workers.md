# RL environment worker deployment

Run one OpenSAC process per worker endpoint. Each instance needs a unique worker ID, data
directory, broker socket and API port; do not put a round-robin proxy in front of stateful
session routes.

Create `configs/worker-0.yaml` and `configs/worker-1.yaml`. The first contains:

```yaml
api:
  port: 8000
storage:
  data_dir: /var/lib/opensac/worker-0
  broker_socket: /var/lib/opensac/worker-0/broker.sock
deployment:
  worker_id: node-a-0
  backend_metadata_hash: sha256:replace-with-index-metadata-hash
sessions:
  max_active: 128
sandbox:
  mode: warm
  max_warm_containers: 32
  max_concurrency: 16
```

The second uses the same fields with port `8001`, worker ID `node-a-1`, and `worker-1` storage
paths. Start each worker with its own file:

```bash
uv run opensac serve --config configs/worker-0.yaml
uv run opensac serve --config configs/worker-1.yaml
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
