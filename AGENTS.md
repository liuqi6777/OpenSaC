# Contributor guidelines

- Communicate with the user in Simplified Chinese. Use English for code, comments and documentation.
- Use four-space indentation, type hints, snake_case functions, PascalCase classes and a
  100-character line limit.
- Keep changes focused. Update affected documentation, examples and configuration templates.
- Test behavior and meaningful failure cases, not source strings or implementation details.
  Avoid redundant tests for low-impact changes.
- For code changes, run:

  ```bash
  uv run pytest
  uv run ruff check .
  uv run ruff format --check .
  uv run mypy src
  ```

- For packaging, dependency or provider-interface changes, also run `uv build` and
  `uv run python scripts/verify_wheels.py`.
- Report what changed, what was verified and any unresolved issues. Do not claim checks that were not run.
- Use concise commit subjects with a body explaining motivation, changes and validation.
- Keep credentials, `.env`, generated outputs and caches out of Git. Preserve licenses and attribution.
- Remove temporary files created during the task when they are no longer needed.
- Do not publish, change remotes or rewrite remote history unless explicitly requested.
