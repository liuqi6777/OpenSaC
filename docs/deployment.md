# Deployment

OpenSAC `v0.8.1` is available as version-matched service and sandbox images on GHCR.
The Docker CLI provides a no-checkout quick start with one YAML profile. Docker Compose remains
available for declarative deployments. Both run the API and capability broker from the service
image and start an isolated sandbox container for each execution. Neither builds nor runs the
`local_search` service. OpenSAC does not publish a PyPI package. For the shortest setup path, use
the main README's [Quick start](../README.md#quick-start-with-docker).

## Before you start

The direct container deployment needs Docker Engine or Docker Desktop, a POSIX-compatible shell,
and permission to run Docker commands. The Compose alternative additionally needs `curl` and
Docker Compose. A source deployment needs Git, Python 3.12+, and `uv`.

- Run setup and sandbox builds from the repository root.
- One OpenSAC process owns one data directory and broker socket. Do not point multiple Uvicorn
  workers at the same paths.
- Sessions are stateful. In a worker pool, later requests must return to the worker that created
  the session.
- Local search has no authentication; keep it on `127.0.0.1` unless the network protects it.

See [RL environment worker deployment](rl-environment-workers.md) for worker-pool details.

## Configuration profiles

All service YAML templates live in `configs/`. Pass exactly one profile with `--config`; omitted
fields retain the validated built-in defaults.

| Profile | Use it for | Important notes |
| --- | --- | --- |
| `configs/local.yaml` | Source development with the local retrieval service | Complete reference profile; state paths resolve to the repository `.opensac` directory. |
| `configs/web.yaml` | Normal Web retrieval with cold, per-execution sandboxes | Requires `OPENSAC_API_KEY`, `OPENSAC_SERPER_API_KEY`, and `OPENSAC_JINA_API_KEY` as needed. |
| `configs/web-performance.yaml` | Eight-core Web deployments where warm sandboxes and a short result cache have been benchmarked | Starts directly with the performance settings; compare it against `web.yaml` on the target host. |
| `configs/docker.yaml` | Docker CLI and Docker Compose deployments | Replace both storage paths with the same absolute host path used for the bind mount; use `darwin` for Docker Desktop on macOS. |

For source deployments, start a profile directly:

```bash
uv run opensac serve --config configs/local.yaml
uv run opensac serve --config configs/web.yaml
```

Only the four API-key variables from `.env` or the real process environment are accepted:
`OPENSAC_API_KEY`, `OPENSAC_MODEL_API_KEY`, `OPENSAC_SERPER_API_KEY`, and
`OPENSAC_JINA_API_KEY`. Other service settings belong in YAML. Unknown or duplicate YAML keys fail
startup, and relative `storage` paths resolve from the selected profile's directory.

## Container deployment

### Direct Docker run

Download and edit the Docker configuration profile. Set both storage paths to the absolute runtime
directory used below, and set `sandbox.docker_host_platform` to `darwin` when using Docker Desktop
for macOS:

```bash
mkdir -p configs
curl -fsSLo configs/docker.yaml \
  https://raw.githubusercontent.com/liuqi6777/OpenSaC/v0.8.1/configs/docker.yaml
```

Export the provider credentials and host-specific runtime values in the current shell:

```bash
export OPENSAC_API_KEY=replace-with-a-long-random-value
export OPENSAC_SERPER_API_KEY=replace-with-serper-key
export OPENSAC_JINA_API_KEY=replace-with-jina-key

export OPENSAC_RUNTIME_DIR="$PWD/opensac-data"
export OPENSAC_DOCKER_SOCKET=/var/run/docker.sock
export OPENSAC_RUN_UID="$(id -u)"
export OPENSAC_RUN_GID="$(id -g)"
if [ "$(uname -s)" = Linux ]; then
  export OPENSAC_DOCKER_GID="$(stat -c '%g' "$OPENSAC_DOCKER_SOCKET")"
else
  export OPENSAC_DOCKER_GID=0
fi
mkdir -p "$OPENSAC_RUNTIME_DIR"
```

If the daemon uses a non-default Unix socket, update `OPENSAC_DOCKER_SOCKET` first. Start the
version-pinned service image:

```bash
docker run --detach \
  --name opensac \
  --init \
  --restart unless-stopped \
  --stop-timeout 180 \
  --user "$OPENSAC_RUN_UID:$OPENSAC_RUN_GID" \
  --group-add "$OPENSAC_DOCKER_GID" \
  --env OPENSAC_API_KEY \
  --env OPENSAC_SERPER_API_KEY \
  --env OPENSAC_JINA_API_KEY \
  --publish 127.0.0.1:8000:8000 \
  --mount "type=bind,source=$PWD/configs/docker.yaml,target=/etc/opensac/opensac.yaml,readonly" \
  --mount "type=bind,source=$OPENSAC_RUNTIME_DIR,target=$OPENSAC_RUNTIME_DIR" \
  --mount "type=bind,source=$OPENSAC_DOCKER_SOCKET,target=/var/run/docker.sock,readonly" \
  --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=64m \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  ghcr.io/liuqi6777/opensac:0.8.1 \
  opensac serve --config /etc/opensac/opensac.yaml
```

After a few seconds, verify the service with the Python runtime already installed in the image:

```bash
docker exec opensac python -c \
  "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/healthz').read().decode())"
```

`docker run` pulls the service image when necessary; the first program execution pulls the
version-matched sandbox image. Only API keys are stored in the container environment, and all
non-secret service settings come from the read-only YAML mount.

Use `docker logs -f opensac`, `docker stop opensac`, and `docker start opensac` for the basic
lifecycle. To recreate or upgrade the service, drain active sessions, remove only the stopped
container with `docker rm opensac`, and repeat the command with matching service and sandbox image
versions. The bind-mounted runtime directory remains on the host.

### Docker Compose alternative

Download the versioned Compose files into an empty deployment directory. This does not clone the
source repository, build an image, or install Python packages:

```bash
mkdir opensac-deploy
cd opensac-deploy
curl -fsSLo compose.yaml \
  https://raw.githubusercontent.com/liuqi6777/OpenSaC/v0.8.1/compose.yaml
curl -fsSLo .env \
  https://raw.githubusercontent.com/liuqi6777/OpenSaC/v0.8.1/.env.example
curl -fsSLo compose.env \
  https://raw.githubusercontent.com/liuqi6777/OpenSaC/v0.8.1/compose.env.example
mkdir -p configs
curl -fsSLo configs/docker.yaml \
  https://raw.githubusercontent.com/liuqi6777/OpenSaC/v0.8.1/configs/docker.yaml
mkdir -p "$PWD/.opensac"
```

Set `OPENSAC_API_KEY`, `OPENSAC_SERPER_API_KEY`, and `OPENSAC_JINA_API_KEY` in `.env`. Then edit
`configs/docker.yaml` so both storage paths equal the absolute `.opensac` path. Edit `compose.env`:

- Set `OPENSAC_CONTAINER_DATA_DIR` to the absolute path printed by `pwd`, followed by `/.opensac`.
- Set `OPENSAC_UID` and `OPENSAC_GID` from `id -u` and `id -g`.
- On Linux, set `OPENSAC_DOCKER_GID` from `stat -c '%g' /var/run/docker.sock`; on Docker Desktop,
  keep it at `0`.
- Keep the service image in `compose.env` and sandbox image in `configs/docker.yaml` on identical
  release versions.

Pull and start the service image:

```bash
docker compose --env-file compose.env pull
docker compose --env-file compose.env up -d
docker compose --env-file compose.env ps
curl -fsS http://127.0.0.1:8000/healthz
```

The first program execution pulls the version-matched sandbox image automatically.

The Compose model has exactly one long-running service, `opensac`. It mounts the Docker socket so
the broker can create short-lived, network-disabled sandbox containers. Docker socket access is
equivalent to host-level Docker control; run this stack only under a trusted account and do not
add the socket to generated programs.

Provider credentials stay in the API container and are never passed to sandboxes. The service
mounts its data directory at the same absolute path inside the container so sibling sandbox
containers can use session workspaces and the broker socket on both Linux and Docker Desktop.

View logs or stop the stack with:

```bash
docker compose --env-file compose.env logs -f opensac
docker compose --env-file compose.env down
```

To upgrade, drain active sessions, update both image tags in `compose.env`, then run `pull` and
`up -d` again. Pin an immutable `X.Y.Z` tag or digest in production.

## Source deployment

### 1. Install

Pin the deployed source revision:

```bash
git clone https://github.com/liuqi6777/OpenSaC.git
cd OpenSaC
git checkout COMMIT_SHA
uv sync --locked
cp .env.example .env
```

Use a dedicated service account for a long-running deployment. Access to the Docker daemon is
highly privileged, so do not share that account with unrelated services.

### 2. Configure a search backend

### Local search

```bash
./local_search/run setup
./local_search/run prepare --revision INDEX_COMMIT_SHA
./local_search/run --host 127.0.0.1 --port 8081
```

Pin `INDEX_COMMIT_SHA` for reproducibility. Start from `configs/local.yaml` and set:

```yaml
deployment:
  backend_revision: replace-with-index-revision
backends:
  search:
    provider: local
    base_url: http://127.0.0.1:8081
  document:
    provider: local
    base_url: http://127.0.0.1:8081
```

The first start downloads and loads `Qwen/Qwen3-Embedding-8B`. Device and index options are in
[Local dense search](local-search.md).

### Web search

Use `configs/web.yaml` and set provider credentials only on the OpenSAC host or in `.env`:

```bash
OPENSAC_SERPER_API_KEY=replace-with-serper-key
OPENSAC_JINA_API_KEY=replace-with-jina-key
```

They remain in the host broker and are not passed to sandbox programs.

To route Serper-compatible search through another endpoint, set its complete request URL in the
backend configuration. OpenSAC uses this value unchanged:

```yaml
backends:
  search:
    provider: serper
    base_url: https://search.example.com/api/search
  document:
    provider: jina
    base_url: https://reader.example.com
```

The document backend uses its configured value unchanged as the Jina Reader prefix and appends
`/{document_url}` for each fetch.

Search and document providers are configured independently but must currently use one supported
source-family pair: `local` + `local`, or `serper` + `jina`. The public session contract continues
to identify those source families as `local` and `web`.

### Rerank service

Rerank is a generic text service that can be shared by Search and Content orchestration. The default
`lexical` backend runs BM25 in process. To replace it with Jina reranking, configure the backend
model and keep the credential in `OPENSAC_JINA_API_KEY`:

```yaml
backends:
  rerank:
    provider: jina
    model: jina-reranker-v3
```

`provider: jina` requires a non-empty model; `provider: lexical` rejects a model. There is no
disabled rerank state. The public session environment reports the selected backend as `lexical` or
`jina`.

### Provider service policies

Search, document fetch, rerank, and LLM completion each bind one execution policy. Global
reliability values under `providers` remain the defaults; override only the service-specific
limits that differ:

```yaml
providers:
  retry_profile: safe
  services:
    search:
      concurrency: 6
      requests_per_second: 2.5
      burst: 2
    document:
      concurrency: 6
      attempt_timeout_seconds: 30
    rerank:
      concurrency: 2
    llm:
      concurrency: 12
      logical_deadline_seconds: 120
```

The fixed service names are `search`, `document`, `rerank`, and `llm`. Provider names and public
backend routes are not policy keys. Rerank policy applies to either lexical or Jina; an LLM policy
override requires its optional backend to be enabled. Older `operation_*` maps are rejected rather
than translated.

### Optional pipeline LLM

Enable the OpenAI-compatible structured extraction backend in YAML, while keeping its credential
in `OPENSAC_MODEL_API_KEY`:

```yaml
backends:
  llm:
    provider: openai_compatible
    model: replace-with-model-name
    base_url: null  # Set for another OpenAI-compatible endpoint.
```

Leave `backends.llm.provider` as `none` when the pipeline LLM capability is not needed. Backend
selection and connection fields live under `backends`; capability policy and admission limits live
under `capabilities.search`, `capabilities.content`, and `capabilities.extraction`.

LLM completion uses the same provider execution path as search, document fetch, and rerank. Its
service policy controls transport retries, timeout, deadline, concurrency, and rate limiting;
pipeline call/output-token budgets and extraction repair remain Capability responsibilities. A
configured `providers.services.llm` override is rejected while the LLM backend is disabled.

### Eight-core Web performance profile

For an eight-core Docker host with at least 8 GB assigned to Docker, start with the following
settings. Warm mode keeps one hardened container per active session but still starts a fresh Python
process for every execution. Eight 512 MB sandbox limits leave room for the service, Docker, and the
128 MB provider cache.

Use the ready-to-run `configs/web-performance.yaml` profile, or copy its `providers`, `sandbox`,
and `limits` sections into a deployment-specific YAML file.

The provider result cache is process-local. Built-in Serper search and Jina document backends opt
successful results into it; local backends do not. It never caches failures, reranker responses,
or LLM output. Keep the TTL at zero when cross-session freshness must take precedence over latency
and provider cost.

The experimental persistent interpreter is a separate opt-in lifecycle and does not use the warm
LRU. Set `sandbox.experimental_persistent_interpreter: true` only for treatment deployments, then
create sessions with `execution_mode="persistent_interpreter"`. Each active treatment session pins
one container until deletion or expiry; size `sandbox.max_concurrency` and host memory for the
maximum concurrent treatment sessions. See [Agent integrations](agent-integrations.md) for
adapter mode selection, explicit REPL skills, loss semantics, and the baseline/treatment split.

### Benchmark before and after tuning

Run the same program against each deployment with identical Docker resources and provider settings.
The following no-op benchmark isolates API, queue, and sandbox overhead and repeats every concurrency
level three times:

```bash
uv run python scripts/benchmark_exec.py \
  --base-url http://127.0.0.1:8000 \
  --concurrency 1,4,8,16 \
  --requests 32 \
  --repetitions 3 \
  --warmup-per-worker 1 \
  --code $'pass\n' \
  --output /tmp/opensac-benchmark.json
```

Use `--code-file` with one fixed Search-as-Code program for the Web path. Compare the `aggregates`
for client P95 and successful requests per second, and use phase timings plus `/healthz` cache/queue
snapshots to attribute the change. Measure `0.6.3` in cold mode before enabling warm mode so the
version upgrade and container reuse remain separately attributable.

### 3. Configure and start OpenSAC

For local-only use, start with `configs/local.yaml`. For a persistent service, copy a profile,
use absolute state paths, and set the service API key in `.env`:

```yaml
api:
  host: 127.0.0.1
  port: 8000
storage:
  data_dir: /var/lib/opensac
  broker_socket: /var/lib/opensac/broker.sock
deployment:
  worker_id: node-a-0
  build_commit: replace-with-git-commit
sessions:
  ttl_seconds: 3600
```

Create the data directory with write permission for the service account, then start OpenSAC:

```bash
uv run opensac serve --config configs/local.yaml
```

For a released revision, the first execution pulls the release-matched sandbox image from GHCR.
Run `uv run opensac build-sandbox --config configs/local.yaml` before `serve` only when testing
unreleased SDK or sandbox changes. The serve command stays in the foreground.

### Minimal systemd service

For a checkout at `/opt/opensac`, create `/etc/systemd/system/opensac.service`:

```ini
[Unit]
Description=OpenSAC API and capability broker
After=network-online.target docker.service
Requires=docker.service

[Service]
User=opensac
Group=opensac
SupplementaryGroups=docker
WorkingDirectory=/opt/opensac
EnvironmentFile=/opt/opensac/.env
ExecStart=/opt/opensac/.venv/bin/opensac serve --config /opt/opensac/configs/local.yaml
Restart=on-failure
RestartSec=5
TimeoutStopSec=180

[Install]
WantedBy=multi-user.target
```

Adjust paths and Docker access for the host. If using local search, manage its foreground command
as a separate service. Enable OpenSAC with:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now opensac.service
```

### 4. Verify and expose

Check the running services:

```bash
curl -fsS http://127.0.0.1:8000/healthz
curl -fsS http://127.0.0.1:8081/healthz  # local backend only
```

OpenSAC `/healthz` does not execute a sandbox or call the provider. Run the Search-as-Code example
in the main README once to verify the complete API, sandbox, broker, and backend path.
The `provider_services` section reports live capacity state for each configured service and route.

The built-in runtime dashboard is available at `http://127.0.0.1:8000/dashboard` when the API is
bound to a loopback address. It shows process and capacity snapshots plus live execution and
capability diagnostics; debug history stays in the open browser tab and is not persisted. To expose
it from a non-loopback deployment, configure both an API key and an explicit opt-in:

```yaml
dashboard:
  enabled: true
```

The page prompts for the API key and keeps it only in the current tab's JavaScript memory. A
non-loopback service without the explicit setting does not mount the dashboard routes; enabling
them without `OPENSAC_API_KEY` is rejected during configuration loading.

When another host needs access, put OpenSAC behind an HTTPS reverse proxy or private network.
Forward `Authorization`, set the proxy read timeout above `sandbox.timeout_seconds`, and
restrict `/healthz`, `/docs`, and `/openapi.json` if operational metadata should not be public.

### 5. Upgrade

Sessions do not survive a worker restart. Drain active work, then update the pinned source:

```bash
read -r -s -p "OpenSAC API key: " OPENSAC_ADMIN_API_KEY
echo
curl -fsS -X POST http://127.0.0.1:8000/v1/admin/drain \
  -H "Authorization: Bearer $OPENSAC_ADMIN_API_KEY"
unset OPENSAC_ADMIN_API_KEY

# Wait until /healthz reports sessions.active == 0.
sudo systemctl stop opensac.service
git fetch --tags
git checkout NEW_COMMIT_SHA
uv sync --locked
# Update sandbox.image in the selected YAML profile to the matching release tag.
sudo systemctl start opensac.service
```

Update `deployment.build_commit` and repeat the end-to-end check. Rollback uses the same steps with
the previous commit.

For a blue-green upgrade, start the new worker on a different port with its own data directory and
broker socket. Send only new sessions to it, keep every existing session pinned to its original
worker, and drain the old worker before removal. Do not place a round-robin proxy in front of
stateful session routes. Keep the previous worker available until the no-op and fixed Web canaries
pass; rollback then consists only of sending new sessions back to that worker.

## Troubleshooting

- Docker permission error: verify the service account can run `docker info`.
- Sandbox contract mismatch: select the image matching the OpenSAC release, or rebuild it from
  the checked-out revision.
- Docker status 125 with `NanoCPUs`: set `sandbox.cpus: 0` if the host has no CPU cgroup.
- `429 capacity_exhausted`: retry according to `Retry-After` or select another worker.
- `410 worker_restarted` or `session_expired`: create a new session.
