# Deployment

OpenSAC `v0.4.0` is publicly available as version-matched service and sandbox images on GHCR.
The Docker CLI provides a no-checkout, no-configuration-file quick start. Docker Compose remains
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

## Container deployment

### Direct Docker run without configuration files

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
  --env OPENSAC_API_HOST=0.0.0.0 \
  --env OPENSAC_API_PORT=8000 \
  --env OPENSAC_SEARCH_BACKEND=web \
  --env OPENSAC_DATA_DIR="$OPENSAC_RUNTIME_DIR" \
  --env OPENSAC_BROKER_SOCKET="$OPENSAC_RUNTIME_DIR/broker.sock" \
  --env OPENSAC_SANDBOX_IMAGE=ghcr.io/liuqi6777/opensac-sandbox:0.4.0 \
  --publish 127.0.0.1:8000:8000 \
  --mount "type=bind,source=$OPENSAC_RUNTIME_DIR,target=$OPENSAC_RUNTIME_DIR" \
  --mount "type=bind,source=$OPENSAC_DOCKER_SOCKET,target=/var/run/docker.sock,readonly" \
  --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=64m \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  ghcr.io/liuqi6777/opensac:0.4.0
```

After a few seconds, verify the service with the Python runtime already installed in the image:

```bash
docker exec opensac python -c \
  "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/healthz').read().decode())"
```

`docker run` pulls the service image when necessary; the first program execution pulls the
version-matched sandbox image. The environment values are stored in the container configuration,
so restrict Docker daemon access just as you would protect an `.env` file.

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
  https://raw.githubusercontent.com/liuqi6777/OpenSaC/v0.4.0/compose.yaml
curl -fsSLo .env \
  https://raw.githubusercontent.com/liuqi6777/OpenSaC/v0.4.0/.env.example
curl -fsSLo compose.env \
  https://raw.githubusercontent.com/liuqi6777/OpenSaC/v0.4.0/compose.env.example
mkdir -p "$PWD/.opensac"
```

Set `OPENSAC_API_KEY`, `OPENSAC_SERPER_API_KEY`, and `OPENSAC_JINA_API_KEY` in `.env`. Keep
`OPENSAC_SEARCH_BACKEND=web`. Then edit `compose.env`:

- Set `OPENSAC_CONTAINER_DATA_DIR` to the absolute path printed by `pwd`, followed by `/.opensac`.
- Set `OPENSAC_UID` and `OPENSAC_GID` from `id -u` and `id -g`.
- On Linux, set `OPENSAC_DOCKER_GID` from `stat -c '%g' /var/run/docker.sock`; on Docker Desktop,
  keep it at `0`.
- Keep the service and sandbox image tags identical to the downloaded release files.

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

Pin `INDEX_COMMIT_SHA` for reproducibility. In `.env`, keep:

```bash
OPENSAC_SEARCH_BACKEND=local
OPENSAC_LOCAL_SEARCH_BASE_URL=http://127.0.0.1:8081
OPENSAC_BACKEND_REVISION=replace-with-index-revision
```

The first start downloads and loads `Qwen/Qwen3-Embedding-8B`. Device and index options are in
[Local dense search](local-search.md).

### Web search

Set provider credentials only on the OpenSAC host:

```bash
OPENSAC_SEARCH_BACKEND=web
OPENSAC_SERPER_API_KEY=replace-with-serper-key
OPENSAC_JINA_API_KEY=replace-with-jina-key
```

They remain in the host broker and are not passed to sandbox programs.

### 3. Configure and start OpenSAC

For local-only use, the defaults in `.env` are sufficient. For a persistent service, use absolute
state paths and set an API key:

```bash
OPENSAC_API_HOST=127.0.0.1
OPENSAC_API_PORT=8000
OPENSAC_API_KEY=replace-with-a-long-random-value
OPENSAC_DATA_DIR=/var/lib/opensac
OPENSAC_BROKER_SOCKET=/var/lib/opensac/broker.sock
OPENSAC_WORKER_ID=node-a-0
OPENSAC_BUILD_COMMIT=replace-with-git-commit
OPENSAC_SESSION_TTL_SECONDS=3600
```

Create the data directory with write permission for the service account, then start OpenSAC:

```bash
uv run opensac serve
```

For a released revision, the first execution pulls the release-matched sandbox image from GHCR.
Run `uv run opensac build-sandbox` before `serve` only when testing unreleased SDK or sandbox
changes. The serve command stays in the foreground.

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
ExecStart=/opt/opensac/.venv/bin/opensac serve
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

When another host needs access, put OpenSAC behind an HTTPS reverse proxy or private network.
Forward `Authorization`, set the proxy read timeout above `OPENSAC_SANDBOX_TIMEOUT_SECONDS`, and
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
# If .env pins OPENSAC_SANDBOX_IMAGE, update it to the new release tag here.
sudo systemctl start opensac.service
```

Update `OPENSAC_BUILD_COMMIT` and repeat the end-to-end check. Rollback uses the same steps with
the previous commit.

## Troubleshooting

- Docker permission error: verify the service account can run `docker info`.
- Sandbox contract mismatch: select the image matching the OpenSAC release, or rebuild it from
  the checked-out revision.
- Docker status 125 with `NanoCPUs`: set `OPENSAC_SANDBOX_CPUS=0` if the host has no CPU cgroup.
- `429 capacity_exhausted`: retry according to `Retry-After` or select another worker.
- `410 worker_restarted` or `session_expired`: create a new session.
