# Release process

OpenSAC uses one `vX.Y.Z` Git tag to publish the service and sandbox container images plus a GitHub
Release. It does not publish or attach Python package distributions. The workflow refuses a tag
that does not exactly match the package version.

## One-time repository setup

GHCR uses the workflow's short-lived `GITHUB_TOKEN`; no registry secret is required. After the
first image publication, make both the `opensac` and `opensac-sandbox` packages public in GitHub
Packages settings so unauthenticated deployments can pull them.

## Version and contract rules

- `src/opensac/_version.py` is the host package version source.
- `packages/opensac-sdk/src/opensac_sdk/_version.py` is the SDK version source.
- Both versions must be identical and use the stable `X.Y.Z` form.
- Release image defaults in `.env.example`, `compose.env.example`, and `compose.yaml` must use that
  same version.
- `SANDBOX_CONTRACT` changes only for an incompatible host/SDK RPC boundary.
- `sandbox/Dockerfile` must carry the same contract default as the runtime.

Run the metadata check after editing either version or the contract:

```bash
uv run python scripts/release.py
```

CI also runs this check on every pull request.

## Publish a release

1. Update both `_version.py` files and the release notes.
2. Run the local release checks:

   ```bash
   uv lock
   uv sync --locked --extra dev
   uv run ruff check .
   uv run pytest
   OPENSAC_DOCKER_E2E=1 uv run pytest tests/test_sandbox_docker_e2e.py
   uv build --all-packages --out-dir dist --clear
   uvx --from twine twine check dist/*
   uv run python scripts/release.py --tag vX.Y.Z
   ```

3. Commit the version change, then create and push an annotated tag:

   ```bash
   git tag -a vX.Y.Z -m "OpenSAC X.Y.Z"
   git push origin main
   git push origin vX.Y.Z
   ```

4. Make the GHCR packages public if this is their first publication.
5. Verify both GHCR manifests and the GitHub Release source archive.

The release publishes these channels for both `opensac` and `opensac-sandbox`:

- `X.Y.Z`: immutable release version and the recommended deployment tag.
- `X.Y`: latest compatible patch in that minor line.
- `sha-...`: source commit traceability.
- `latest`: convenience channel for the newest stable release.

The sandbox image additionally publishes `contract-N`, the latest image implementing sandbox
contract `N`. No local-search image is built or published.

Production deployments should pin `X.Y.Z` or an image digest. Never move or reuse an existing
release tag; publish a patch version to correct a broken release.
