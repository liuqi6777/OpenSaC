from __future__ import annotations

import re
from pathlib import Path

from opensac.sandbox.validator import validate_code

ROOT = Path(__file__).parents[1]
BASELINE_DIR = ROOT / ".agents" / "skills" / "search-as-code"
MCP_DIR = ROOT / ".agents" / "skills" / "search-as-code-repl"
CLI_DIR = ROOT / ".agents" / "skills" / "search-as-code-repl-cli"
REFERENCE_NAMES = ("sdk-contract.md", "orchestration.md", "repeated-units.md")


def _linked_references(skill: str) -> set[str]:
    return set(re.findall(r"\((references/[^)]+\.md)(?:#[^)]+)?\)", skill))


def _cli_invocation() -> str:
    skill = (CLI_DIR / "SKILL.md").read_text(encoding="utf-8")
    shell = skill.split("```bash", 1)[1].split("```", 1)[0].strip().splitlines()
    assert shell[0] == "opensac agent-run <<'PY'"
    assert shell[-1] == "PY"
    return "\n".join(shell[1:-1])


def test_repl_skills_preserve_separate_adapter_surfaces() -> None:
    mcp_skill = (MCP_DIR / "SKILL.md").read_text(encoding="utf-8")
    cli_skill = (CLI_DIR / "SKILL.md").read_text(encoding="utf-8")

    assert "name: search-as-code-repl" in mcp_skill.split("---", 2)[1]
    assert "sac_run(code)" in mcp_skill
    assert "agent-run" not in mcp_skill

    assert "name: search-as-code-repl-cli" in cli_skill.split("---", 2)[1]
    assert "opensac agent-run <<'PY'" in cli_skill
    assert "uv run opensac agent-run" in cli_skill
    assert "sac_run(code)" not in cli_skill

    expected_links = {f"references/{name}" for name in REFERENCE_NAMES}
    for skill in (mcp_skill, cli_skill):
        assert "`persistent_interpreter`" in skill
        assert "observations intentionally omit execution status metadata" in skill
        assert "interpreter_state=ready" not in skill
        assert "sdk.workspace" in skill
        assert "4,000" in skill
        assert _linked_references(skill) == expected_links
        assert "patterns.md" not in skill


def test_repl_skill_metadata_disables_implicit_invocation() -> None:
    expected = {
        MCP_DIR: ("Search as Code REPL", "$search-as-code-repl"),
        CLI_DIR: ("Search as Code REPL CLI", "$search-as-code-repl-cli"),
    }
    for skill_dir, (display_name, invocation) in expected.items():
        metadata = (skill_dir / "agents" / "openai.yaml").read_text(encoding="utf-8")
        assert f'display_name: "{display_name}"' in metadata
        assert invocation in metadata
        assert "allow_implicit_invocation: false" in metadata


def test_repl_references_are_self_contained_and_synchronized() -> None:
    for name in REFERENCE_NAMES:
        canonical = (BASELINE_DIR / "references" / name).read_bytes()
        assert (MCP_DIR / "references" / name).read_bytes() == canonical
        assert (CLI_DIR / "references" / name).read_bytes() == canonical

    for skill_dir in (MCP_DIR, CLI_DIR):
        assert not (skill_dir / "references" / "patterns.md").exists()


def test_repl_cli_invocation_compiles_and_passes_sandbox_validation() -> None:
    invocation = _cli_invocation()
    compile(invocation, "<search-as-code-repl-cli-invocation>", "exec")
    validate_code(invocation)


def test_baseline_skill_catalog_metadata_remains_implicit() -> None:
    for name in ("search-as-code", "search-as-code-cli"):
        metadata = (ROOT / ".agents" / "skills" / name / "agents" / "openai.yaml").read_text(
            encoding="utf-8"
        )
        assert "allow_implicit_invocation" not in metadata
