# Agent integrations

[English](agent-integrations.md) | [简体中文](agent-integrations.zh-CN.md)

OpenSAC provides the execution runtime, not the control loop. An agent integration should expose
one model-visible operation, `sac_run(code)`, and reuse one OpenSAC session for the same rollout or
conversation.

## Choose an integration

| Integration | Use when | Context/session owner |
| --- | --- | --- |
| Custom HTTP/Python loop | You own the agent harness | Your application |
| `opensac agent-run` | A coding agent can execute shell commands | CLI adapter |
| `opensac mcp` | Codex should call one MCP tool | MCP adapter |

The control-model endpoint used by an external agent is separate from the optional pipeline-model
endpoint exposed inside sandbox programs as `sdk.llm.*`.

## Prerequisites

Start the public `v0.8.0` service image by following the main README's
[Docker quick start](../README.md#quick-start-with-docker). The service itself needs no source
checkout. PyPI publication is not planned, so a host that uses the CLI or MCP adapter should check
out the matching release to install the adapter and skills:

```bash
git clone https://github.com/liuqi6777/OpenSaC.git
cd OpenSaC
git checkout v0.8.0
uv tool install --editable '/absolute/path/to/OpenSaC[mcp]'

export SAC_API_BASE=http://127.0.0.1:8000
export SAC_API_KEY=replace-with-your-opensac-key
```

The skills are versioned in the repository rather than embedded in the Python wheel. Set
`OPENSAC_REPO` to the version-matched checkout:

```bash
export OPENSAC_REPO=/absolute/path/to/OpenSaC
```

Never place a literal API key in a committed project configuration. Reference an environment
variable or use a user-local configuration instead.

The base package is sufficient for `opensac agent-run`. The MCP adapter requires the `mcp` extra,
the bundled control loop requires `agent`, and a source service configured with `OPENSAC_MODEL_NAME`
requires `llm`; `full` installs all three profiles.

## Custom agent loop

Wrap `POST /v1/sessions/{session_id}/exec`, or `OpenSAC.exec_code`, as a single `sac_run(code)`
tool. Create one session when the rollout starts, reuse it across turns, and delete or abort it at
the end. The default `execution_mode="program"` preserves workspace files and session-bound local
document IDs while keeping session IDs out of model-generated arguments. Public source URLs can be
carried across sessions. The experimental alternative is
`OpenSAC.create_session(execution_mode="persistent_interpreter")`, described below.

Render the execution response's bounded `warnings` list before stdout. These warnings expose
partial or complete external item failures even when generated code only prints successful values;
they do not make an otherwise successful execution fail.

The runnable [sac_agent](../sac_agent/README.md) package demonstrates a minimal OpenAI-compatible
ReAct loop. For production harnesses, also handle leases, `worker_restarted`, `session_expired`,
request idempotency, and worker affinity.

## Choose project or global scope

The adapter command is installed once per user. Skills and MCP registration can be limited to one
project or exposed to every project for that user:

| Host | Project scope | Global/user scope |
| --- | --- | --- |
| Codex skill | `<project>/.agents/skills/` | `~/.agents/skills/` |
| Codex MCP | `<project>/.codex/config.toml` | user Codex configuration |
| Claude Code skill | `<project>/.claude/skills/` | `~/.claude/skills/` |

Use project scope for a team repository or when OpenSAC should be visible only in one project. Use
global scope for a personal installation shared across repositories.

This repository uses `.agents/skills/` as the canonical skill source. Its `.claude/skills`
directory points to the same source, so CLI skill changes do not need to be maintained twice.

## Pure CLI integration

The CLI path uses the `search-as-code-cli` skill and pipes each program to `opensac agent-run`.

### Install the CLI skill

`AGENT_PROJECT` is the repository where the coding agent will use OpenSAC:

```bash
export AGENT_PROJECT=/absolute/path/to/your/project

# Project scope — run the copy for each host you use.
mkdir -p "$AGENT_PROJECT/.agents/skills" "$AGENT_PROJECT/.claude/skills"
cp -R "$OPENSAC_REPO/.agents/skills/search-as-code-cli" "$AGENT_PROJECT/.agents/skills/"
cp -R "$OPENSAC_REPO/.agents/skills/search-as-code-cli" "$AGENT_PROJECT/.claude/skills/"

# Global scope — run the copy for each host you use.
mkdir -p ~/.agents/skills ~/.claude/skills
cp -R "$OPENSAC_REPO/.agents/skills/search-as-code-cli" ~/.agents/skills/
cp -R "$OPENSAC_REPO/.agents/skills/search-as-code-cli" ~/.claude/skills/
```

Test the adapter directly:

```bash
opensac agent-run <<'OPENSAC_PY'
from opensac_sdk import sdk
print(sdk.search("OpenSAC Search as Code", limit=3))
OPENSAC_PY
```

Local Codex tasks use `CODEX_THREAD_ID`; Claude Code shells use `CLAUDE_CODE_SESSION_ID`. If neither
is available, the command fails closed rather than sharing a session by process or working
directory. Other CLI agents must set an explicit conversation identity:

```bash
export SAC_AGENT_CONTEXT_ID=stable-conversation-id
export SAC_AGENT_HOST=my-agent
```

### CLI settings and lifecycle

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `SAC_API_BASE` | `http://127.0.0.1:8000` | OpenSAC API used by the adapter |
| `SAC_API_KEY` | empty, then `OPENSAC_API_KEY` | Bearer credential; never stored in the registry |
| `SAC_CLI_EXECUTION_MODE` | `program` | Session execution mode; treatment uses `persistent_interpreter` |
| `SAC_CLI_LEASE_SECONDS` | `3600` | Renewable session lease, from `1` to `86400` seconds |
| `SAC_CLI_STATE_DIR` | platform user-state directory | CLI SQLite generation registry |
| `SAC_AGENT_CONTEXT_ID` | unset | Explicit conversation ID for another CLI agent |
| `SAC_AGENT_HOST` | `cli` | Namespace paired with an explicit context ID |

The adapter derives the raw conversation ID with SHA-256 and a host namespace before persistence.
When one invocation exits, it closes its HTTP client but leaves the leased service session
resumable. If the service reports `session_expired` or `worker_restarted`, the failed program is
not replayed: the adapter returns `state_lost`, and the next call starts a clean generation.

## MCP integration

The Codex MCP path uses the `search-as-code` skill and exposes only `sac_run(code)`. Claude Code
uses the CLI path above because it does not provide conversation identity in MCP request metadata.

### Install the MCP skill

```bash
export AGENT_PROJECT=/absolute/path/to/your/project

# Project scope.
mkdir -p "$AGENT_PROJECT/.agents/skills"
cp -R "$OPENSAC_REPO/.agents/skills/search-as-code" "$AGENT_PROJECT/.agents/skills/"

# Global scope.
mkdir -p ~/.agents/skills
cp -R "$OPENSAC_REPO/.agents/skills/search-as-code" ~/.agents/skills/
```

### Codex MCP

For project scope, merge this into `<project>/.codex/config.toml`. Codex loads project
configuration only after the project is trusted. Export `SAC_API_KEY` before launching Codex:

```toml
[mcp_servers.opensac]
command = "opensac"
args = ["mcp"]
env_vars = ["SAC_API_KEY"]

[mcp_servers.opensac.env]
SAC_API_BASE = "http://127.0.0.1:8000"
```

For global/user scope:

```bash
codex mcp add opensac \
  --env SAC_API_BASE="$SAC_API_BASE" \
  --env SAC_API_KEY="$SAC_API_KEY" \
  -- opensac mcp
```

Codex supplies the current task identity in MCP request metadata. If it is absent, the adapter
fails closed and does not fall back to a process-wide or working-directory session.

### MCP settings and lifecycle

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `SAC_API_BASE` | `http://127.0.0.1:8000` | OpenSAC API used by the adapter |
| `SAC_API_KEY` | empty, then `OPENSAC_API_KEY` | Bearer credential; never stored in the MCP registry |
| `SAC_MCP_EXECUTION_MODE` | `program` | Session execution mode; treatment uses `persistent_interpreter` |
| `SAC_MCP_LEASE_SECONDS` | `3600` | Renewable session lease, from `1` to `86400` seconds |
| `SAC_MCP_STATE_DIR` | platform user-state directory | MCP SQLite generation registry |

Raw Codex task IDs are SHA-256-derived with a host namespace before they enter request IDs or
SQLite. A task reuses one leased OpenSAC session and can recover it after an MCP restart. MCP
shutdown closes HTTP clients without deleting sessions. On `session_expired` or
`worker_restarted`, the failed program is not replayed; the current call returns `state_lost`, and
the next call starts a clean generation.

## Experimental persistent interpreter

This feature is opt-in on both the service and adapter. Enable it only for an isolated experiment:

```bash
# OpenSAC service configuration
export OPENSAC_EXPERIMENTAL_PERSISTENT_INTERPRETER=true

# Choose exactly one adapter for the treatment process.
export SAC_CLI_EXECUTION_MODE=persistent_interpreter
# or: export SAC_MCP_EXECUTION_MODE=persistent_interpreter
```

Each treatment session lazily starts one internal `default` Python interpreter. Top-level
variables, functions, imports, and assignments completed before an ordinary exception survive the
next `sac_run` call. `mechanisms.persistence` still controls files only: when it is false, each cell
gets a temporary workspace while Python globals remain alive. Persistent sessions pin their
sandbox container until deletion or expiry, so capacity planning must count concurrent treatment
sessions rather than the warm-container LRU limit.

Install the matching skill alongside the baseline skill:

```bash
# MCP treatment skill (Codex)
cp -R "$OPENSAC_REPO/.agents/skills/search-as-code-repl" "$AGENT_PROJECT/.agents/skills/"

# CLI treatment skill (Codex or Claude Code)
cp -R "$OPENSAC_REPO/.agents/skills/search-as-code-repl-cli" "$AGENT_PROJECT/.agents/skills/"
cp -R "$OPENSAC_REPO/.agents/skills/search-as-code-repl-cli" "$AGENT_PROJECT/.claude/skills/"
```

The REPL skills disable implicit invocation. Prompt the treatment explicitly with
`$search-as-code-repl` or `$search-as-code-repl-cli`; continue using the existing skill and
`program` mode for the baseline. The first observation reports the actual execution mode, and the
REPL skill treats a mismatch as a configuration error.

Responses report `interpreter_state` as `not_started`, `ready`, or `lost` plus an optional loss
reason and a count of top-level user symbols. Symbol names and values are not recorded. A timeout,
output overflow, kernel exit, protocol failure, or leftover background thread marks the interpreter
lost and removes its container. The failed cell is never replayed, later direct execution returns
`410 interpreter_lost`, and adapters rotate to a clean session on their next invocation.

## Security and correctness rules

- Expose only `sac_run(code)` to the model; keep context binding and credentials host-side.
- Reuse a session only for one trusted conversation or rollout identity.
- Do not persist raw conversation IDs or API keys in project files or adapter registries.
- Do not automatically replay an execution after state loss; the previous result may be
  indeterminate.
- Keep the adapter and service on compatible OpenSAC versions.

See the official [Codex MCP](https://developers.openai.com/codex/mcp) and
[Codex skills](https://developers.openai.com/codex/skills) documentation for host-specific
configuration behavior.
