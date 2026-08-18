from __future__ import annotations

from pathlib import Path

from opensac.sandbox.validator import validate_code

SKILL_DIR = Path(__file__).parents[1] / ".agents" / "skills" / "search-as-code-cli"
SKILL_PATH = SKILL_DIR / "SKILL.md"


def _example_program() -> str:
    skill = SKILL_PATH.read_text(encoding="utf-8")
    shell_block = skill.split("```bash", 1)[1].split("```", 1)[0].strip()
    lines = shell_block.splitlines()
    assert lines[0] == "opensac agent-run <<'OPENSAC_PY'"
    assert lines[-1] == "OPENSAC_PY"
    return "\n".join(lines[1:-1])


def test_cli_skill_is_small_and_keeps_lifecycle_out_of_the_prompt() -> None:
    skill = SKILL_PATH.read_text(encoding="utf-8")

    assert len(skill) < 7_000
    assert "opensac agent-run <<'OPENSAC_PY'" in skill
    assert "Never create or manage REST sessions" in skill
    assert "state_lost" in skill
    assert "not replayed" in skill
    assert "SAC_API_" not in skill
    assert "SAC_CLI_" not in skill
    assert "SAC_AGENT_" not in skill
    assert "CODEX_THREAD_ID" not in skill
    assert "CLAUDE_CODE_SESSION_ID" not in skill
    assert "/v1/sessions" not in skill
    assert "bind_context" not in skill
    assert "SQLite" not in skill
    assert "lease" not in skill


def test_cli_skill_example_compiles_and_passes_sandbox_validation() -> None:
    program = _example_program()

    compile(program, "<search-as-code-cli-skill>", "exec")
    validate_code(program)
    assert "sdk.search.many(" in program
    assert "sdk.search.fuse_rrf(" in program
    assert "sdk.content.grep_report(" in program
    assert "passage.locator is not None" in program


def test_cli_skill_has_codex_catalog_metadata() -> None:
    metadata = (SKILL_DIR / "agents" / "openai.yaml").read_text(encoding="utf-8")

    assert 'display_name: "Search as Code CLI"' in metadata
    assert "$search-as-code-cli" in metadata
