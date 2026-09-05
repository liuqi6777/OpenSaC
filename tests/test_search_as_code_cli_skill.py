from __future__ import annotations

import re
from pathlib import Path

from opensac.sandbox.validator import validate_code

ROOT = Path(__file__).parents[1]
SKILL_DIR = ROOT / ".agents" / "skills" / "search-as-code-cli"
MCP_SKILL_DIR = ROOT / ".agents" / "skills" / "search-as-code"
SKILL_PATH = SKILL_DIR / "SKILL.md"
REFERENCE_NAMES = ("sdk-contract.md", "orchestration.md", "repeated-units.md")


def _fenced_blocks(path: Path, fence: str) -> list[str]:
    text = path.read_text(encoding="utf-8")
    return [part.split("```", 1)[0].strip() for part in text.split(f"```{fence}")[1:]]


def _posix_program() -> str:
    blocks = _fenced_blocks(SKILL_PATH, "bash")
    assert len(blocks) == 1
    lines = blocks[0].splitlines()
    assert lines[0] == "opensac agent-run <<'PY'"
    assert lines[-1] == "PY"
    return "\n".join(lines[1:-1])


def test_cli_skill_preserves_its_adapter_boundary_and_routes_shared_references() -> None:
    skill = SKILL_PATH.read_text(encoding="utf-8")
    frontmatter = skill.split("---", 2)[1]
    linked_references = set(re.findall(r"\((references/[^)]+\.md)(?:#[^)]+)?\)", skill))

    assert "name: search-as-code-cli" in frontmatter
    assert "shell-capable environments" in frontmatter
    assert "opensac agent-run <<'PY'" in skill
    assert "uv run opensac agent-run" in skill
    assert "sdk.workspace" in skill
    assert "state_lost" in skill
    assert "4,000" in skill
    assert linked_references == {f"references/{name}" for name in REFERENCE_NAMES}
    assert "patterns.md" not in skill


def test_cli_skill_invocation_compiles_and_passes_sandbox_validation() -> None:
    program = _posix_program()
    compile(program, "<search-as-code-cli-invocation>", "exec")
    validate_code(program)


def test_cli_references_match_the_canonical_skill_and_helpers_compile() -> None:
    for name in REFERENCE_NAMES:
        cli_reference = SKILL_DIR / "references" / name
        canonical_reference = MCP_SKILL_DIR / "references" / name
        assert cli_reference.read_bytes() == canonical_reference.read_bytes()

    assert not (SKILL_DIR / "references" / "patterns.md").exists()
    for name in ("orchestration.md", "repeated-units.md"):
        for index, program in enumerate(_fenced_blocks(SKILL_DIR / "references" / name, "python")):
            compile(program, f"<{name}:{index}>", "exec")
            validate_code(program)


def test_claude_code_project_uses_cli_instead_of_removed_mcp_binding() -> None:
    assert not (ROOT / ".mcp.json").exists()
    settings_path = ROOT / ".claude" / "settings.json"
    if settings_path.exists():
        settings = settings_path.read_text(encoding="utf-8")
        assert "mcp__opensac__sac_run" not in settings
        assert "bind_context" not in settings


def test_cli_skill_has_codex_catalog_metadata() -> None:
    metadata = (SKILL_DIR / "agents" / "openai.yaml").read_text(encoding="utf-8")

    assert 'display_name: "Search as Code CLI"' in metadata
    assert "$search-as-code-cli" in metadata
