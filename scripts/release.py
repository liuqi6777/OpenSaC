from __future__ import annotations

import argparse
import ast
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
STABLE_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
DEPENDENCY_NAME = re.compile(r"[A-Za-z0-9_.-]+")


class ReleaseValidationError(ValueError):
    pass


@dataclass(frozen=True)
class ReleaseMetadata:
    version: str
    sandbox_contract: int


def _dependency_names(dependencies: list[str]) -> set[str]:
    return {
        match.group(0).lower().replace("_", "-")
        for dependency in dependencies
        if (match := DEPENDENCY_NAME.match(dependency)) is not None
    }


def _literal_assignment(path: Path, name: str) -> str | int:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if isinstance(target, ast.Name) and target.id == name:
            value = ast.literal_eval(node.value)
            if isinstance(value, str | int):
                return value
    raise ReleaseValidationError(f"Could not find literal {name} in {path.relative_to(REPO_ROOT)}")


def validate_release(tag: str | None = None) -> ReleaseMetadata:
    opensac_version = _literal_assignment(REPO_ROOT / "src/opensac/_version.py", "__version__")
    sdk_version = _literal_assignment(
        REPO_ROOT / "packages/opensac-sdk/src/opensac_sdk/_version.py",
        "__version__",
    )
    contract = _literal_assignment(REPO_ROOT / "src/opensac/sandbox/docker.py", "SANDBOX_CONTRACT")
    if not isinstance(opensac_version, str) or not isinstance(sdk_version, str):
        raise ReleaseValidationError("Package versions must be strings")
    if not isinstance(contract, int):
        raise ReleaseValidationError("SANDBOX_CONTRACT must be an integer")
    if opensac_version != sdk_version:
        raise ReleaseValidationError(
            f"opensac version {opensac_version!r} does not match opensac-sdk {sdk_version!r}"
        )
    if STABLE_VERSION.fullmatch(opensac_version) is None:
        raise ReleaseValidationError(
            f"Release version {opensac_version!r} must use the stable X.Y.Z format"
        )
    root_project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]
    sdk_project = tomllib.loads(
        (REPO_ROOT / "packages/opensac-sdk/pyproject.toml").read_text(encoding="utf-8")
    )["project"]
    if "opensac-sdk" in _dependency_names(root_project["dependencies"]):
        raise ReleaseValidationError("opensac must not depend on the sandbox client SDK")
    if "opensac" in _dependency_names(sdk_project["dependencies"]):
        raise ReleaseValidationError("opensac-sdk must not depend on the host package")
    if tag is not None and tag != f"v{opensac_version}":
        raise ReleaseValidationError(
            f"Git tag {tag!r} does not match package version 'v{opensac_version}'"
        )

    dockerfile = (REPO_ROOT / "sandbox/Dockerfile").read_text(encoding="utf-8")
    contract_arg = re.search(r"^ARG OPENSAC_SANDBOX_CONTRACT=([0-9]+)$", dockerfile, re.M)
    if contract_arg is None or int(contract_arg.group(1)) != contract:
        rendered = contract_arg.group(1) if contract_arg is not None else "missing"
        raise ReleaseValidationError(
            f"sandbox/Dockerfile contract {rendered!r} does not match runtime contract {contract}"
        )

    image_tag_pattern = re.compile(
        r"ghcr\.io/liuqi6777/opensac(?:-sandbox)?:([0-9]+\.[0-9]+\.[0-9]+)"
    )
    for relative_path in (".env.example", "compose.env.example", "compose.yaml"):
        configured_tags = image_tag_pattern.findall(
            (REPO_ROOT / relative_path).read_text(encoding="utf-8")
        )
        if not configured_tags:
            raise ReleaseValidationError(f"Could not find a release image tag in {relative_path}")
        stale_tags = sorted({value for value in configured_tags if value != opensac_version})
        if stale_tags:
            raise ReleaseValidationError(
                f"{relative_path} image tags {stale_tags!r} do not match package version "
                f"{opensac_version!r}"
            )

    return ReleaseMetadata(version=opensac_version, sandbox_contract=contract)


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate OpenSAC release metadata.")
    parser.add_argument("--tag", help="Require this Git tag to match v<package-version>.")
    parser.add_argument(
        "--field",
        choices=("version", "contract"),
        help="Print one machine-readable field instead of the validation summary.",
    )
    args = parser.parse_args()
    try:
        metadata = validate_release(args.tag)
    except ReleaseValidationError as exc:
        parser.exit(1, f"release validation failed: {exc}\n")
    if args.field == "version":
        print(metadata.version)
    elif args.field == "contract":
        print(metadata.sandbox_contract)
    else:
        print(
            f"release metadata valid: version={metadata.version} "
            f"sandbox_contract={metadata.sandbox_contract}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
