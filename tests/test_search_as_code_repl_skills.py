from __future__ import annotations

from pathlib import Path

from opensac.sandbox.validator import validate_code

ROOT = Path(__file__).parents[1]
MCP_DIR = ROOT / ".agents" / "skills" / "search-as-code-repl"
CLI_DIR = ROOT / ".agents" / "skills" / "search-as-code-repl-cli"
REFERENCE_NAMES = (
    "advanced.md",
    "patterns.md",
    "python-recipes.md",
    "sdk-contract.md",
    "stateful-research.md",
)


def _python_blocks(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    return [part.split("```", 1)[0].strip() for part in text.split("```python")[1:]]


def _cli_invocation() -> str:
    skill = (CLI_DIR / "SKILL.md").read_text(encoding="utf-8")
    shell = skill.split("```bash", 1)[1].split("```", 1)[0].strip().splitlines()
    assert shell[0] == "opensac agent-run <<'OPENSAC_PY'"
    assert shell[-1] == "OPENSAC_PY"
    return "\n".join(shell[1:-1])


def test_repl_skills_are_explicit_and_use_separate_adapter_surfaces() -> None:
    mcp_skill = (MCP_DIR / "SKILL.md").read_text(encoding="utf-8")
    cli_skill = (CLI_DIR / "SKILL.md").read_text(encoding="utf-8")

    assert "name: search-as-code-repl" in mcp_skill.split("---", 2)[1]
    assert "$search-as-code-repl" in mcp_skill.split("---", 2)[1]
    assert "sac_run(code)" in mcp_skill
    assert "agent-run" not in mcp_skill

    assert "name: search-as-code-repl-cli" in cli_skill.split("---", 2)[1]
    assert "$search-as-code-repl-cli" in cli_skill.split("---", 2)[1]
    assert "opensac agent-run <<'OPENSAC_PY'" in cli_skill
    assert "sac_run(code)" not in cli_skill

    for skill in (mcp_skill, cli_skill):
        assert "execution_mode=persistent_interpreter" in skill
        assert "interpreter_state=ready" in skill
        assert "sdk.session.usage()" in skill
        assert "sdk.output.submit(...)" in skill
        assert "NEXT:" in skill
        assert "checkpoint" in skill
        assert "never" in skill.lower() and "replay" in skill.lower()
        assert "/v1/sessions" not in skill


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
        mcp_reference = MCP_DIR / "references" / name
        cli_reference = CLI_DIR / "references" / name
        assert mcp_reference.is_file()
        assert mcp_reference.read_bytes() == cli_reference.read_bytes()

    combined = "\n".join(
        (MCP_DIR / "references" / name).read_text(encoding="utf-8") for name in REFERENCE_NAMES
    )
    assert "sdk.search.many" in combined
    assert "sdk.content.passages" in combined
    assert "sdk.state.write_json" in combined
    assert "sdk.output.submit" in combined
    assert "interpreter_lost" in combined
    assert "Python variables do not survive calls" not in combined


def test_repl_examples_compile_and_pass_sandbox_validation() -> None:
    invocation = _cli_invocation()
    compile(invocation, "<search-as-code-repl-cli-invocation>", "exec")
    validate_code(invocation)

    for reference_name in ("patterns.md", "python-recipes.md", "stateful-research.md"):
        blocks = _python_blocks(MCP_DIR / "references" / reference_name)
        assert blocks
        for index, program in enumerate(blocks):
            compile(program, f"<{reference_name}:{index}>", "exec")
            validate_code(program)


def test_baseline_skill_catalog_metadata_remains_implicit() -> None:
    for name in ("search-as-code", "search-as-code-cli"):
        metadata = (ROOT / ".agents" / "skills" / name / "agents" / "openai.yaml").read_text(
            encoding="utf-8"
        )
        assert "allow_implicit_invocation" not in metadata
