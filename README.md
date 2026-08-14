# OpenSAC

**An open, inspectable implementation of Search as Code for research agents.**

[English](README.md) | [简体中文](README.zh-CN.md)

OpenSAC turns search from a fixed tool call into a programmable interface. An external agent
writes Python to compose retrieval, document inspection, filtering, ranking, structured
extraction, and citation. OpenSAC executes that program in an isolated Docker sandbox and
mediates every privileged operation through a host-side capability broker.

The project is designed for controlled research on a central question:

> When the model, retrieval backend, and evaluation protocol are held constant, does composing
> search primitives in generated code improve task quality, context efficiency, or the
> latency–cost trade-off over composing the same primitives through model-visible tool calls?

OpenSAC implements the public [Search as Code](https://research.perplexity.ai/articles/rethinking-search-as-code-generation)
abstraction. It is not a reconstruction of Perplexity's internal search engine.

> [!IMPORTANT]
> OpenSAC is an active research prototype (software version `0.4.0`). APIs, documentation, and
> research artifacts may continue to evolve.

## Highlights

- **Programmable search pipelines** — agents can batch queries, fuse rankings, filter and join
  records, inspect coverage, and select evidence with ordinary Python.
- **Compact typed SDK** — search, content, state, optional structured LLM extraction, usage, and
  citation primitives are available through `opensac_sdk`.
- **Hardened execution** — generated programs run without network access, provider credentials,
  the Docker socket, or unrestricted host filesystem access.
- **Context decoupling** — large intermediate results stay in the program workspace; only
  explicitly printed or submitted data returns to the control model.
- **Provenance-preserving evidence** — opaque session-scoped references and broker-issued passage
  locators connect retrieved candidates to final citations.
- **Research instrumentation** — per-session budgets, typed partial failures, capability traces,
  phase timings, idempotent execution, and worker lifecycle controls support reproducible
  rollouts.
- **Backend-neutral deployment** — use the included dense local retriever or web retrieval through
  Serper and Jina Reader without changing generated programs.

## System design

```text
external agent / rollout harness
              |
              | generated Python via POST /exec
              v
       OpenSAC session API  ---- persistent workspace and refs
              |
              v
     isolated Docker sandbox
              |
              | opensac_sdk over authenticated Unix-socket RPC
              v
     host capability broker
        /          |           \
 local search   web search   optional pipeline LLM
```

OpenSAC deliberately does **not** own the agent loop. The external control plane selects the
model, generates programs, manages rollouts, and evaluates answers. One rollout should reuse one
OpenSAC session so workspace files and opaque document references remain valid across turns.
Backend choice, credentials, retries, rate limits, and resource enforcement remain server-side.

See [Design goals and capability roadmap](docs/design.md) for the full research rationale and
[OpenSAC 0.4](docs/opensac-0.4.md) for the current capability contract.

## Repository layout

| Path | Purpose |
| --- | --- |
| `src/opensac/` | HTTP API, Python client, capability broker, backends, sandbox, and metrics |
| `packages/opensac-sdk/` | Typed SDK embedded in generated programs |
| `sandbox/` | Hardened Docker image and sandbox entrypoint |
| `sac_agent/` | Minimal ReAct control agent exposing one `sac_run(code)` tool |
| `local_search/` | Standalone FAISS dense-retrieval service |
| `skills/search-as-code/` | Search-as-Code skill for coding agents |
| `examples/` | Example SDK programs and local runners |
| `tests/` | Unit, integration, security, and Docker end-to-end tests |
| `docs/` | Design, deployment, instrumentation, and release documentation |
| `paper/opensac/` | Work-in-progress manuscript source |

## Requirements

- Python 3.12 or newer
- [`uv`](https://docs.astral.sh/uv/)
- Docker for isolated execution
- One search backend:
  - the included local retriever, its FAISS index, and sufficient RAM/GPU resources; or
  - Serper and Jina API credentials for web retrieval
- Optional: an OpenAI-compatible chat-completions endpoint for `sdk.llm.*`

## Quick start

### 1. Install

```bash
git clone https://github.com/liuqi6777/OpenSaC.git
cd OpenSaC
uv sync --extra dev
cp .env.example .env
```

Review `.env` before deployment. An empty `OPENSAC_API_KEY` is suitable only for trusted local
development; set a strong bearer token whenever the API is exposed beyond localhost.

### 2. Configure one search backend

#### Option A: local dense retrieval

The included service loads a prepared BrowseComp-Plus FAISS index. It does not train or rebuild
the index.

```bash
./local_search/run setup
./local_search/run prepare --revision COMMIT_SHA  # pin a revision for reproducibility
./local_search/run
```

The last command stays in the foreground on `127.0.0.1:8081`. In `.env`, keep:

```bash
OPENSAC_SEARCH_BACKEND=local
OPENSAC_LOCAL_SEARCH_BASE_URL=http://127.0.0.1:8081
```

The first launch also downloads `Qwen/Qwen3-Embedding-8B`. CPU execution is supported but needs
substantial RAM and is much slower. See [Local dense search](docs/local-search.md) for exact data
contracts, device selection, and health checks.

#### Option B: web retrieval

```bash
export OPENSAC_SEARCH_BACKEND=web
export OPENSAC_SERPER_API_KEY=your-serper-key
export OPENSAC_JINA_API_KEY=your-jina-key
```

The web backend uses Serper for result retrieval and Jina Reader for document content. Keep these
credentials on the OpenSAC host; never place them in generated programs.

### 3. Build and start OpenSAC

```bash
uv run opensac build-sandbox
uv run opensac serve
```

Rebuild the sandbox image after upgrading the repository. In another terminal, verify the
service:

```bash
curl -fsS http://127.0.0.1:8000/healthz
```

### 4. Execute a Search-as-Code program

The following example creates one session, runs generated-style Python, prints the structured
result, and always deletes the session:

```bash
uv run python - <<'PY'
import os

from opensac import OpenSAC

program = '''
from opensac_sdk import sdk

batches = sdk.search.many(
    ["ReAct paper", "ReAct reasoning acting language models"],
    limit_per_query=5,
    concurrency=2,
)
fusion = sdk.search.fuse_rrf(batches, k=60, limit=5)
refs = [candidate.ref for candidate in fusion.candidates]
passages = sdk.content.snippets("Who introduced ReAct?", refs, max_tokens=2000)

sdk.output.submit(
    {"passages": [passage.model_dump() for passage in passages]},
    citations=[
        {"ref": passage.ref, "locator": passage.locator}
        for passage in passages
        if passage.locator is not None
    ],
)
'''

with OpenSAC(api_key=os.getenv("OPENSAC_API_KEY", "")) as client:
    session = client.create_session()
    try:
        result = client.exec_code(session["id"], program, include_trace=True)
        print(result["output"])
        print(result["usage"])
    finally:
        client.delete_session(session["id"])
PY
```

For a richer pipeline with multi-query fusion, source filtering, persistent JSONL state, and
passage citations, see [`examples/research_pipeline.py`](examples/research_pipeline.py). To
iterate on an SDK program without a control model, use:

```bash
uv run python examples/run_sdk_locally.py examples/research_pipeline.py
# Add --docker to exercise the real sandbox and validator.
```

Host mode is for development only: it does not apply container isolation or sandbox validation.

## SDK surface

Generated programs import the singleton with `from opensac_sdk import sdk`.

| Namespace | Main operations | Role |
| --- | --- | --- |
| `sdk.search` | `search(...)`, `many(...)`, `fuse_rrf(...)` | Retrieve and fuse candidates while preserving provenance |
| `sdk.content` | `get_many(...)`, `snippets(...)`, `grep(...)`, `grep_report(...)`, `read(...)` | Fetch, locate, and inspect evidence |
| `sdk.llm` | `map(...)`, `map_many(...)`, `extract(...)`, `extract_many(...)` | Optional brokered model calls and schema-checked extraction |
| `sdk.state` | JSON/JSONL and workspace helpers | Persist explicit state across executions in one session |
| `sdk.session` | `usage()` | Inspect compact strategy counts and remaining budgets |
| `sdk.output` | `submit(...)` | Return structured output and resolve trusted citations |

Batch operations preserve input alignment and expose typed per-row failures. Empty search results
are successful results, not failures. Passage-level citations require the locator returned by a
content operation; clients must not manufacture locators. The complete behavior and migration
notes are documented in [OpenSAC 0.4](docs/opensac-0.4.md).

## Agent integrations

OpenSAC can be driven in two ways:

1. **Custom agent loop.** Wrap `/v1/sessions/{session_id}/exec` as one model-visible
   `sac_run(code)` tool and reuse a session for the rollout. The runnable
   [`sac_agent`](sac_agent/README.md) package demonstrates a minimal OpenAI-compatible ReAct loop.
2. **Coding agent.** Install or adapt [`skills/search-as-code`](skills/search-as-code/SKILL.md) so
   a coding agent can generate programs and call a deployed OpenSAC service directly.

The control-model endpoint used by `sac_agent` is separate from the optional pipeline-model
endpoint exposed inside the sandbox as `sdk.llm.*`.

## TODO

- Publish benchmark protocols, reproducible experiment configurations, traces, and results.
- Add a stable paper citation after the manuscript is released.

## Documentation

- [Design goals and capability roadmap](docs/design.md)
- [OpenSAC 0.4 release and migration notes](docs/opensac-0.4.md)
- [Local dense search](docs/local-search.md)
- [Research instrumentation](docs/research-instrumentation.md)
- [RL worker deployment](docs/rl-environment-workers.md)
- [Tool capability gaps](docs/tool-capability-gaps.md)
- [High-fan-out reliability plan](docs/opensac-0.3-plan.md)

## Limitations

- OpenSAC is a research runtime, not a hosted search product or a complete agent framework.
- Real isolation requires Docker. The host-mode SDK runner is not a security boundary.
- The included local retriever uses a large embedding model and a prepared index; it may be
  impractical on resource-constrained machines.
- Web retrieval quality, availability, latency, and cost depend on external providers.
- The sandbox reduces risk but does not replace host hardening, network controls, authentication,
  monitoring, or filesystem quotas in a multi-tenant deployment.

## Contributing

Issues and focused pull requests are welcome. Please keep changes small, document behavior changes,
and add or update tests where applicable. Before opening a pull request, run:

```bash
uv run ruff check .
uv run pytest
```

For changes to public SDK behavior, also update the capability contract or release notes under
`docs/`.

## Citation

If OpenSAC supports your research, cite the repository while the paper is under review:

```bibtex
@software{liu2026opensac,
  author  = {Qi Liu},
  title   = {OpenSAC: An Open Implementation of Search as Code},
  year    = {2026},
  url     = {https://github.com/liuqi6777/OpenSaC},
  version = {0.4.0}
}
```

Please also cite the original Search-as-Code work when discussing the architecture.

## License

OpenSAC is released under the [MIT License](LICENSE).
