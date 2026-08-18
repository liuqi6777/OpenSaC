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
| `opensac mcp` | Codex or Claude Code should call one MCP tool | MCP adapter |

The control-model endpoint used by an external agent is separate from the optional pipeline-model
endpoint exposed inside sandbox programs as `sdk.llm.*`.

## Prerequisites

Start the OpenSAC API from a source checkout first. Public Compose images are not available until
the first release, and PyPI publication is not planned. Install the host-side adapter from the
same checkout:

```bash
uv tool install --editable /absolute/path/to/OpenSaC

export SAC_API_BASE=http://127.0.0.1:8000
export SAC_API_KEY=replace-with-your-opensac-key
```

The skills are versioned in the repository rather than embedded in the Python wheel. Set
`OPENSAC_REPO` to the current checkout; after Docker releases exist, check out the same tag as the
running service:

```bash
export OPENSAC_REPO=/absolute/path/to/OpenSaC
```

Never place a literal API key in a committed project configuration. Reference an environment
variable or use a user-local configuration instead.

## Custom agent loop

Wrap `POST /v1/sessions/{session_id}/exec`, or `OpenSAC.exec_code`, as a single `sac_run(code)`
tool. Create one session when the rollout starts, reuse it across turns, and delete or abort it at
the end. This preserves workspace files and opaque document references while keeping session IDs
out of model-generated arguments.

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
| Claude Code MCP | `<project>/.mcp.json` | user configuration with `--scope user` |

Use project scope for a team repository or when OpenSAC should be visible only in one project. Use
global scope for a personal installation shared across repositories.

This repository uses `.agents/skills/` as the canonical skill source. Its `.claude/skills`
directory points to the same source, so changes do not need to be maintained twice.

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
| `SAC_CLI_LEASE_SECONDS` | `3600` | Renewable session lease, from `1` to `86400` seconds |
| `SAC_CLI_STATE_DIR` | platform user-state directory | CLI SQLite generation registry |
| `SAC_AGENT_CONTEXT_ID` | unset | Explicit conversation ID for another CLI agent |
| `SAC_AGENT_HOST` | `cli` | Namespace paired with an explicit context ID |

The adapter derives the raw conversation ID with SHA-256 and a host namespace before persistence.
When one invocation exits, it closes its HTTP client but leaves the leased service session
resumable. If the service reports `session_expired` or `worker_restarted`, the failed program is
not replayed: the adapter returns `state_lost`, and the next call starts a clean generation.

## MCP integration

The MCP path uses the `search-as-code` skill. The public tool surface is `sac_run(code)`;
conversation binding and lifecycle tools are host-internal.

### Install the MCP skill

```bash
export AGENT_PROJECT=/absolute/path/to/your/project

# Project scope — run the copy for each host you use.
mkdir -p "$AGENT_PROJECT/.agents/skills" "$AGENT_PROJECT/.claude/skills"
cp -R "$OPENSAC_REPO/.agents/skills/search-as-code" "$AGENT_PROJECT/.agents/skills/"
cp -R "$OPENSAC_REPO/.agents/skills/search-as-code" "$AGENT_PROJECT/.claude/skills/"

# Global scope — run the copy for each host you use.
mkdir -p ~/.agents/skills ~/.claude/skills
cp -R "$OPENSAC_REPO/.agents/skills/search-as-code" ~/.agents/skills/
cp -R "$OPENSAC_REPO/.agents/skills/search-as-code" ~/.claude/skills/
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

### Claude Code MCP

For project scope, add this to `<project>/.mcp.json`. The key is expanded from each user's
environment and is not committed literally:

```json
{
  "mcpServers": {
    "opensac": {
      "type": "stdio",
      "command": "opensac",
      "args": ["mcp"],
      "env": {
        "SAC_API_BASE": "${SAC_API_BASE:-http://127.0.0.1:8000}",
        "SAC_API_KEY": "${SAC_API_KEY}"
      }
    }
  }
}
```

Claude Code asks for approval before using a project-scoped MCP server. For global/user scope:

```bash
claude mcp add \
  --scope user \
  --env SAC_API_BASE="$SAC_API_BASE" \
  --env SAC_API_KEY="$SAC_API_KEY" \
  --transport stdio \
  opensac \
  -- opensac mcp
```

Claude Code also supports `--scope local` for a project-specific configuration stored outside the
repository.

Finally, merge this context-binding hook into `<project>/.claude/settings.json` or
`~/.claude/settings.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "mcp__opensac__sac_run",
        "hooks": [
          {
            "type": "mcp_tool",
            "server": "opensac",
            "tool": "bind_context",
            "input": { "context_id": "${session_id}" }
          }
        ]
      }
    ]
  }
}
```

If the hook does not bind a context, `sac_run` fails closed. The Search-as-Code skill reserves
`bind_context` for the host hook; the model must not call it directly.

### MCP settings and lifecycle

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `SAC_API_BASE` | `http://127.0.0.1:8000` | OpenSAC API used by the adapter |
| `SAC_API_KEY` | empty, then `OPENSAC_API_KEY` | Bearer credential; never stored in the MCP registry |
| `SAC_MCP_LEASE_SECONDS` | `3600` | Renewable session lease, from `1` to `86400` seconds |
| `SAC_MCP_STATE_DIR` | platform user-state directory | MCP SQLite generation registry |

Raw Codex and Claude conversation IDs are SHA-256-derived with a host namespace before they enter
request IDs or SQLite. A task reuses one leased OpenSAC session and can recover it after an MCP
restart. MCP shutdown closes HTTP clients without deleting sessions. On `session_expired` or
`worker_restarted`, the failed program is not replayed; the current call returns `state_lost`, and
the next call starts a clean generation.

## Security and correctness rules

- Expose only `sac_run(code)` to the model; keep context binding and credentials host-side.
- Reuse a session only for one trusted conversation or rollout identity.
- Do not persist raw conversation IDs or API keys in project files or adapter registries.
- Do not automatically replay an execution after state loss; the previous result may be
  indeterminate.
- Keep the adapter and service on compatible OpenSAC versions.

See the official [Codex MCP](https://developers.openai.com/codex/mcp),
[Codex skills](https://developers.openai.com/codex/skills),
[Claude Code MCP](https://code.claude.com/docs/en/mcp), and
[Claude Code hooks](https://code.claude.com/docs/en/hooks) documentation for host-specific
configuration behavior.
