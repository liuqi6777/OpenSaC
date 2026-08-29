from __future__ import annotations

import re
from pathlib import Path

import pytest

import scripts.release as release_module
from scripts.release import ReleaseValidationError, _dependency_names, validate_release


def test_release_metadata_is_consistent() -> None:
    metadata = validate_release()

    assert metadata.version == "0.8.3"
    assert metadata.capability_contract == 15
    assert metadata.sandbox_contract == 14
    assert validate_release(f"v{metadata.version}") == metadata


def test_release_tag_must_match_package_version() -> None:
    with pytest.raises(ReleaseValidationError, match="does not match package version"):
        validate_release("v9.9.9")


def test_release_rejects_sandbox_contract_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def write(relative_path: str, content: str) -> None:
        path = tmp_path / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    write("src/opensac/_version.py", '__version__ = "0.8.2"\n')
    write(
        "packages/opensac-sdk/src/opensac_sdk/_version.py",
        '__version__ = "0.8.2"\nCAPABILITY_CONTRACT = 13\n',
    )
    write("src/opensac/models.py", "CAPABILITY_CONTRACT = 13\n")
    write("src/opensac/sandbox/docker_core.py", "SANDBOX_CONTRACT = 14\n")
    write("pyproject.toml", "[project]\ndependencies = []\n")
    write("packages/opensac-sdk/pyproject.toml", "[project]\ndependencies = []\n")
    write("sandbox/Dockerfile", "ARG OPENSAC_SANDBOX_CONTRACT=13\n")
    monkeypatch.setattr(release_module, "REPO_ROOT", tmp_path)

    with pytest.raises(ReleaseValidationError, match="runtime contract 14"):
        release_module.validate_release()


def test_release_dependency_names_ignore_versions_and_extras() -> None:
    assert _dependency_names(["OpenSAC_SDK[http]>=0.6", "opensac @ file:///tmp/host"]) == {
        "opensac-sdk",
        "opensac",
    }


def test_github_actions_are_pinned_to_commits() -> None:
    workflows = Path(__file__).resolve().parents[1] / ".github/workflows"
    action_refs = [
        line.split("@", 1)[1].split()[0]
        for workflow in workflows.glob("*.yml")
        for line in workflow.read_text(encoding="utf-8").splitlines()
        if line.lstrip().startswith("uses:")
    ]

    assert action_refs
    assert all(re.fullmatch(r"[0-9a-f]{40}", ref) for ref in action_refs)


def test_release_publishes_service_and_sandbox_images() -> None:
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github/workflows/release.yml").read_text(encoding="utf-8")
    compose = (root / "compose.yaml").read_text(encoding="utf-8")
    configuration_profiles = [
        path.read_text(encoding="utf-8") for path in sorted((root / "configs").glob("*.yaml"))
    ]
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")

    assert "ghcr.io/${{ github.repository_owner }}/opensac\n" in workflow
    assert "ghcr.io/${{ github.repository_owner }}/opensac-sandbox\n" in workflow
    assert "gh-action-pypi-publish" not in workflow
    assert "Publish opensac to PyPI" not in workflow
    assert "python-distributions" not in workflow
    assert "/dist/opensac-*.whl" in dockerfile
    assert "/dist/*.whl" not in dockerfile
    assert "  local_search:" not in compose
    assert "  local-search:" not in compose
    assert configuration_profiles
    assert all("ghcr.io/liuqi6777/opensac-sandbox:0.8.3" in text for text in configuration_profiles)
