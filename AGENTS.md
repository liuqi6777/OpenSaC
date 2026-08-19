# Repository Guidelines

## Project Structure & Module Organization

- `src/opensac/` contains the main Python package: API, client, capability broker, sandbox, providers, backends, and metrics.
- `packages/opensac-sdk/` is the workspace package embedded in generated programs.
- `tests/` contains unit, integration, security, and Docker end-to-end tests; shared fixtures/data live under `tests/data/`.
- `sandbox/` defines the isolated Docker image and entrypoint. `local_search/` is the standalone dense-retrieval service.
- `examples/`, `docs/`, `sac_agent/`, and `paper/opensac/` contain runnable examples, project documentation, the minimal agent, and manuscript sources.

## Build, Test, and Development Commands

Use Python 3.12+ and run commands from the repository root:

```bash
uv sync --all-packages --extra dev  # Install all workspace and development dependencies
uv run pytest                       # Run the full test suite
uv run pytest tests/test_api.py     # Run a focused test module
uv run ruff check .                 # Run lint checks
uv run ruff format --check .        # Verify formatting
uv run opensac build-sandbox        # Build the execution sandbox image
uv run opensac serve                # Start the local API service
```

Docker is required for sandbox and end-to-end tests. Configure local credentials and backend settings in `.env`, based on `.env.example`; never commit secrets.

## Coding Style & Naming Conventions

Follow the existing Python style: four-space indentation, type hints, small explicit functions, and descriptive `snake_case` names. Use `PascalCase` for classes and `UPPER_SNAKE_CASE` for constants. Keep lines at 100 characters. Run Ruff before submitting changes; its configured rules cover errors, imports, modern Python syntax, bug-prone patterns, and simplification.

## Testing Guidelines

Tests use `pytest` with automatic `pytest-asyncio` support. Name files `test_<area>.py` and test functions `test_<behavior>`. Add regression coverage for behavior changes, including failure paths and broker/sandbox boundaries where relevant. Run focused tests while iterating, then `uv run pytest` before opening a PR.

## Commit & Pull Request Guidelines

Use concise imperative commit subjects with an optional category prefix, matching project history (for example, `fix: handle broker socket` or `feat: add local retriever`). Every human-authored commit must also include a detailed body that explains the motivation, summarizes the important changes, and lists the validation performed. Note configuration or security implications when relevant. Keep commits focused.

When publishing a version update, first run the release checks in `docs/releasing.md` and commit all version and release-note changes. After that commit is present on the target release branch (normally `main`), create an annotated `vX.Y.Z` tag that points to the exact release commit and atomically push the branch and tag together, for example `git push --atomic origin main vX.Y.Z`. A tag push triggers the release workflow, so never tag an unmerged feature or pull-request branch. Verify that the tag does not already exist, and never move or reuse a published tag.

PRs should explain the problem, summarize the design, list validation commands, note configuration or security implications, and link the related issue or experiment when applicable. Include logs, screenshots, or reproducibility details for CLI, API, Docker, or documentation changes.

## Security & Configuration Tips

Treat generated code and provider credentials as untrusted/sensitive. Preserve the sandbox and broker isolation model, avoid logging tokens or document contents, and update `.env.example` when adding configuration keys.
