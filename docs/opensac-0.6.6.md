# OpenSAC v0.6.6

OpenSAC v0.6.6 reduces the default host installation while preserving the complete service image
and all generated-program capabilities.

## Optional integration profiles

The base `opensac` package now contains the API, broker, search backends, sandbox runtime, and CLI
without installing pipeline-model, MCP, or control-agent dependencies. Install only the profile a
deployment uses:

```bash
pip install 'opensac[llm]'    # sdk.llm.* pipeline-model capabilities
pip install 'opensac[mcp]'    # opensac mcp
pip install 'opensac[agent]'  # python -m sac_agent
pip install 'opensac[full]'   # all optional integrations
```

Source development remains `uv sync --locked --all-packages --extra dev`; the development profile
includes all optional runtime dependencies. The published service container installs the `llm`
profile so existing model-enabled deployments retain their behavior.

Requesting an integration without its extra now fails at that integration boundary with the exact
install command. A base service with no `OPENSAC_MODEL_NAME` does not import OpenAI or jsonschema.

## Internal compatibility cleanup

Provider concurrency continues to be controlled by `OPENSAC_BACKEND_FETCH_CONCURRENCY` and the
per-operation provider policies. The ignored `fetch_concurrency` arguments have been removed from
the internal Serper and local-search backend constructors.

Custom provider adapters must raise `ProviderRequestError` for transport or provider failures.
Returning a successful `ContentSnippet` with `metadata["fetch_error"]` is no longer interpreted as
a failure.

## Deployment compatibility

The sandbox contract remains `12` and the capability contract remains `11`; no generated-program
wire shape changed. Deploy matching v0.6.6 service and sandbox images. Existing deployments that
use only the published containers require no configuration migration.
