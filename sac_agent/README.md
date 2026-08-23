# Minimal Search-as-Code Agent

`sac_agent` is a small, runnable control plane for OpenSAC. It gives an
OpenAI-compatible model exactly one native tool, `sac_run(code)`, and lets the
model research by composing OpenSAC SDK calls inside Python programs.

The package intentionally contains only:

- a native tool-calling model client;
- a minimal ReAct loop;
- the `sac_run` OpenSAC HTTP adapter;
- a CLI entry point.

It does not contain evaluation code, datasets, experiment logging, hooks,
context management, subagents, or backend-specific retrieval logic.

## How it works

1. The ReAct loop sends the question and the `sac_run` schema to the control
   model.
2. The model writes a Python research program using `opensac_sdk`.
3. `sac_run` lazily creates one OpenSAC session and executes the program in its
   sandbox.
4. Compact stdout, stderr, submitted output, usage, citations, and workspace
   filenames are returned to the model as a tool observation.
5. Later tool calls reuse the same session, so local sources and workspace files survive across
   turns. Public web URLs can also be reused across sessions. The session is deleted when the run ends.

The agent treats OpenSAC as a black box. Session creation sends `{}`: the agent
does not select, inspect, or expose the search backend. Configure retrieval and
its credentials on the OpenSAC server.

## Requirements

- Python 3.12 or newer;
- a running OpenSAC server with a configured search backend;
- Docker on the OpenSAC host for real sandbox execution;
- an OpenAI-compatible chat-completions endpoint with native function calling.

The control-model endpoint and the optional `sdk.llm.*` endpoint are separate.
The former runs this ReAct loop; the latter, if enabled, is configured on the
OpenSAC server and is called only from generated sandbox programs.

## Quick start

Install the repository dependencies:

```bash
uv sync --extra agent
```

The `agent` profile installs the OpenAI-compatible control-model client. Repository contributors
can use `uv sync --extra dev`, which also includes the test and lint toolchain.

Configure and start OpenSAC from the repository root:

```bash
cp .env.example .env
uv run opensac build-sandbox
uv run opensac serve
```

Backend setup, including local retrieval and web search, is documented in the
[main OpenSAC README](../README.md#quick-start). The agent does not need to know which
backend the server uses.

In another shell, configure the control model:

```bash
export AGENT_MODEL_NAME=your-model
export AGENT_API_KEY=your-key
export AGENT_API_BASE=http://127.0.0.1:8001/v1

export SAC_API_BASE=http://127.0.0.1:8000
# export SAC_API_KEY=your-opensac-key
```

Run a question:

```bash
uv run python -m sac_agent \
  "Which paper introduced the ReAct agent pattern?"
```

When the model returns a non-empty message without a tool call, that complete
message becomes the final answer and is written to stdout. No answer tags are
required or stripped. A run that times out, reaches the turn limit, or fails to
produce an answer exits with a non-zero status and reports its termination
reason.

## CLI

```text
python -m sac_agent [--max-turns N] [--timeout SECONDS] QUESTION
```

- `--max-turns` defaults to `32` model turns.
- `--timeout` defaults to `1800` seconds for the whole ReAct run.

Example:

```bash
uv run python -m sac_agent \
  --max-turns 20 \
  --timeout 900 \
  "Compare the original ReAct and Toolformer papers."
```

## Configuration

Only model identity/API connection and OpenSAC API connection are configurable
through the environment:

| Variable | Fallback | Required | Purpose |
| --- | --- | --- | --- |
| `AGENT_MODEL_NAME` | `OPENAI_MODEL` | Yes | Control-model name |
| `AGENT_API_KEY` | `OPENAI_API_KEY`, then `EMPTY` | Usually | Control-model credential |
| `AGENT_API_BASE` | `OPENAI_BASE_URL`, then the OpenAI SDK default | No | Chat-completions base URL |
| `SAC_API_BASE` | `http://127.0.0.1:8000` | No | OpenSAC API URL |
| `SAC_API_KEY` | `OPENSAC_API_KEY`, then empty | No | OpenSAC bearer token |

`SAC_BACKENDS` is deliberately unsupported. The OpenSAC service owns backend
selection.

### Fixed runtime policy

To keep this minimal implementation reproducible, the following values are
constants in code rather than environment settings:

| Component | Setting | Value |
| --- | --- | --- |
| Model client | request timeout | 600 seconds |
| Model client | additional retries | 3 |
| Model request | temperature | 1.0 |
| Model request | top-p | 0.95 |
| Model request | presence penalty | 0.0 |
| Model request | maximum output tokens | 16384 |
| `sac_run` | HTTP timeout | 300 seconds |
| `sac_run` | observation output limit | 32000 characters |

Environment variables such as `AGENT_TEMPERATURE`, `AGENT_MAX_TOKENS`,
`AGENT_EXTRA_BODY`, `SAC_TIMEOUT_SECONDS`, and `SAC_OUTPUT_LIMIT` are ignored.

## Python API

Synchronous usage:

```python
from sac_agent import ReactAgent

result = ReactAgent().run("What evidence supports the requested claim?")
print(result.answer)
print(result.termination, result.turns)
```

Explicit configuration without environment variables:

```python
from sac_agent import ModelClient, ModelConfig, ReactAgent, SacConfig, SacRunTool

client = ModelClient(
    ModelConfig(
        model="your-model",
        api_key="your-key",
        base_url="http://127.0.0.1:8001/v1",
    )
)
tool = SacRunTool(
    SacConfig(
        api_base="http://127.0.0.1:8000",
        api_key="your-opensac-key",
    )
)
result = ReactAgent(client=client, tool=tool).run("Research this question.")
print(result.answer)
```

Asynchronous usage:

```python
import asyncio

from sac_agent import ReactAgent


async def main() -> None:
    agent = ReactAgent()
    try:
        result = await agent.arun("Research this question.")
        print(result.answer)
    finally:
        await agent.aclose()


asyncio.run(main())
```

`AgentResult` contains `answer`, the full `messages` transcript, the number of
`turns`, and `termination`. Normal completion uses `termination="answer"`;
other values include `timeout`, `max_turns`, and `invalid_response`.

## Session and output behavior

- The OpenSAC session is created on the first valid `sac_run` call, not when
  the agent object is constructed.
- One agent run uses one session. Workspace files and local sources remain valid across its tool
  calls; public web URLs remain reusable after session loss.
- The session is deleted in a `finally` block when the run ends. A deletion
  failure is recorded as `SacRunTool.close_error` and does not replace an
  answer already produced.
- Raw search results and page bodies stay inside the sandbox. Only what the
  generated program prints or submits is returned to the control model.
- The observation renderer gives one 32000-character pool to stdout, stderr,
  and submitted output, and keeps both ends when truncation is necessary.

For the SDK surface and recommended program shape, see the
[Search-as-Code skill](../.agents/skills/search-as-code/SKILL.md).

## Troubleshooting

### `Set AGENT_MODEL_NAME (or OPENAI_MODEL)`

Configure a control-model name. For a local OpenAI-compatible server, also set
`AGENT_API_BASE`; most such servers accept `AGENT_API_KEY=EMPTY` when they do
not require authentication.

### Model rejects `tools` or never calls `sac_run`

The endpoint must support native OpenAI-style function calling. A text-only
chat-completions implementation is not sufficient for this agent.

### `[sac_run] OpenSAC request failed`

Check that `SAC_API_BASE` points to the OpenSAC API, the bearer key matches the
server configuration, and the server can create a sandbox session. OpenSAC
health and backend setup are covered by the main repository documentation.

### Search or `sdk.llm.*` fails inside a program

Those capabilities are owned by OpenSAC. Configure the search backend and any
pipeline-model credentials on the server; do not pass those credentials to the
control agent or generated code.

## Development

Run the focused tests:

```bash
uv run pytest -q tests/test_sac_agent.py
```

Run lint and the complete repository test suite:

```bash
uv run ruff check sac_agent tests/test_sac_agent.py
uv run pytest -q
```
