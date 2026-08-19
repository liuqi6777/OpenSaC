from __future__ import annotations

import re
from pathlib import Path

import pytest

from scripts.release import ReleaseValidationError, _dependency_names, validate_release


def test_release_metadata_is_consistent() -> None:
    metadata = validate_release()

    assert validate_release(f"v{metadata.version}") == metadata


def test_release_tag_must_match_package_version() -> None:
    with pytest.raises(ReleaseValidationError, match="does not match package version"):
        validate_release("v9.9.9")


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
