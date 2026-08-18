# opensac-dsh

[English](README.md) | [简体中文](README.zh-CN.md)

`opensac-dsh` is an installable DeepSeek Harness bundle that exposes OpenSAC as one native,
model-visible tool:

```text
sac_run({ code: string }) -> string
```

The plugin supplies the capability; its bundled `search-as-code-dsh` skill teaches the model how
to compose evidence-grounded programs. The skill does not invoke a shell or manage sessions.

## Why a plugin instead of a standalone skill

A standalone skill can only instruct the model to use a generic shell tool. This plugin can bind
the current `exec.agent.id` to a persistent OpenSAC session, pass cancellation to the child process,
resolve credentials through dsh, enforce a fixed no-shell command, and keep stateful calls
exclusive. These are runtime guarantees rather than prompt conventions.

## Prerequisites

- DeepSeek Harness `0.1.0-rc.7`-compatible packages, Node.js `^22.19.0` or `>=24.0.0`, and
  pnpm `11.7.0`.
- A running OpenSAC `0.4.x` service.
- The matching `opensac` CLI installed on the same host as dsh.

OpenSAC is not published to PyPI, so install its CLI from a version-matched checkout:

```bash
export OPENSAC_REPO=/absolute/path/to/OpenSaC
uv tool install --editable "$OPENSAC_REPO"
```

## Build and install

Build the TypeScript package before adding its local checkout to a dsh profile:

```bash
cd "$OPENSAC_REPO/integrations/deepseek-harness"
corepack pnpm install --frozen-lockfile
corepack pnpm build

dsh plugin --profile web add "$OPENSAC_REPO/integrations/deepseek-harness"
```

Replace `web` with the profile that should receive the tool. For a portable artifact, run
`corepack pnpm pack` after the build and add the resulting `.tgz` instead.

Export service configuration before starting dsh. Omit `SAC_API_KEY` only when the OpenSAC service
is intentionally unauthenticated:

```bash
export SAC_API_BASE=http://127.0.0.1:8000
export SAC_API_KEY=replace-with-your-opensac-key
dsh --profile web
```

The bundle contributes a Cordis row with id `opensac`. A later profile patch can replace its
configuration. dsh patch rows replace `config` wholesale, so include every non-default value you
need when overriding the row.

## Configuration

| Field | Default | Purpose |
| --- | --- | --- |
| `command` | `opensac` | Absolute executable or bare PATH name, resolved at plugin load |
| `apiBase` | `http://127.0.0.1:8000` | OpenSAC HTTP(S) service base |
| `apiKeyEnv` | `SAC_API_KEY` | dsh credential reference, resolved once per tool call |
| `leaseSeconds` | `3600` | Renewable CLI session lease, from 1 to 86400 seconds |
| `stateDir` | OpenSAC platform default | Optional CLI SQLite generation-registry directory |
| `cwd` | dsh launch directory | Child process working directory |
| `timeoutMs` | `310000` | Cooperative dsh tool timeout |
| `maxOutputBytes` | `262144` | Per-stream stdout/stderr retention cap |
| `graceMs` | `1000` | SIGTERM-to-SIGKILL process-tree grace |

`SAC_API_BASE`, `SAC_CLI_LEASE_SECONDS`, and `SAC_CLI_STATE_DIR` seed the bundle defaults through
`cordis.patch.yml`. `apiKeyEnv` names a credential; no literal key is stored in the patch.

## Lifecycle and security

- The public tool surface is only `sac_run(code)`. Context ids and credentials are host-owned.
- The CLI runs as fixed argv (`opensac agent-run`) with the program on stdin; no shell parses model
  content.
- dsh scrubs the ambient child environment. The plugin explicitly forwards only OpenSAC settings
  and the credential resolved for that call.
- Calls are exclusive because `sac_run` intentionally does not opt into dsh concurrent execution.
- Output is bounded. Truncated observations fail instead of returning incomplete evidence.
- The existing OpenSAC CLI registry hashes the raw dsh agent id before persistence and renews the
  leased service session across calls and dsh restarts.
- `state_lost` is returned as an observation and is never replayed automatically. Removing the
  plugin also does not delete a resumable OpenSAC session; lease expiry owns cleanup.

## Development

```bash
corepack pnpm typecheck
corepack pnpm test
corepack pnpm build
corepack pnpm publint
```

Tests are local to this package and use a structural subprocess fake; they do not require a live
dsh or OpenSAC service.
