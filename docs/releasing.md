# Publishing to PyPI

## One-time setup

Create a pending publisher at https://pypi.org/manage/account/publishing/ while
signed in to the PyPI account that should own the new project:

| Field | Value |
| --- | --- |
| PyPI project name | `opensac` |
| Owner | `liuqi6777` |
| Repository name | `OpenSaC` |
| Workflow name | `publish.yml` |
| Environment name | `pypi` |

If the project already exists under your account, add the publisher from the
project's Publishing settings instead. A pending publisher does not reserve a name.
Create a GitHub environment named `pypi` in the repository settings. No PyPI API
token or GitHub secret is needed.

See the [PyPI setup guide](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/)
and [uv publishing guide](https://docs.astral.sh/uv/guides/integration/github/#publishing-to-pypi).

## Release a version

1. Update the version with `uv version 0.1.0` (substitute the intended version).
   Update the README's installation instructions and release-related documentation.
2. Commit `pyproject.toml`, `uv.lock` and documentation changes, push to `main`,
   and wait for CI to pass.
3. Create a new annotated tag matching the version on that exact `main` commit,
   for example `git tag -a v0.1.0 -m "Release 0.1.0"`, then `git push origin v0.1.0`.
   Never move or reuse a published version tag.
4. Publish a GitHub Release for the tag. Publishing the Release triggers
   `publish.yml`; pushing a tag or saving a draft Release alone does not upload to PyPI.
   Prereleases also trigger publishing, so use a matching Python prerelease version
   such as `0.1.0rc1` and tag `v0.1.0rc1`.
5. Check the Publish to PyPI workflow and verify installation outside the checkout:

   ```bash
   uv run --no-project --with opensac==0.1.0 python -c "from opensac import sdk"
   ```

The workflow runs the full CI matrix on the release commit, checks that the commit
belongs to `main` and that the tag matches the package version, then builds and
verifies the distributions before uploading them using PyPI Trusted Publishing.
The version in `pyproject.toml` is authoritative; publishing does not change it.
