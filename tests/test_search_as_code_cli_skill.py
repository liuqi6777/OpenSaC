from __future__ import annotations

from pathlib import Path

from opensac.sandbox.validator import validate_code

ROOT = Path(__file__).parents[1]
SKILL_DIR = ROOT / ".agents" / "skills" / "search-as-code-cli"
MCP_SKILL_DIR = ROOT / ".agents" / "skills" / "search-as-code"
SKILL_PATH = SKILL_DIR / "SKILL.md"
CONTRACT_PATH = SKILL_DIR / "references" / "sdk-contract.md"
ADVANCED_PATH = SKILL_DIR / "references" / "advanced.md"
PATTERNS_PATH = SKILL_DIR / "references" / "patterns.md"
RECIPES_PATH = SKILL_DIR / "references" / "python-recipes.md"
STATEFUL_PATH = SKILL_DIR / "references" / "stateful-research.md"


def _fenced_block(path: Path, fence: str, *, heading: str | None = None) -> str:
    text = path.read_text(encoding="utf-8")
    if heading is not None:
        text = text.split(heading, 1)[1]
    return text.split(f"```{fence}", 1)[1].split("```", 1)[0].strip()


def _fenced_blocks(path: Path, fence: str) -> list[str]:
    return [part.split("```", 1)[0].strip() for part in path.read_text().split(f"```{fence}")[1:]]


def _posix_program() -> str:
    lines = _fenced_block(SKILL_PATH, "bash").splitlines()
    assert lines[0] == "opensac agent-run <<'OPENSAC_PY'"
    assert lines[-1] == "OPENSAC_PY"
    return "\n".join(lines[1:-1])


def test_cli_skill_is_small_host_neutral_and_routes_details() -> None:
    skill = SKILL_PATH.read_text(encoding="utf-8")

    assert len(skill) < 6_500
    assert "opensac agent-run <<'OPENSAC_PY'" in skill
    assert "Never create or manage REST sessions" in skill
    assert "stable `research_id`" in skill
    assert "state_lost" in skill
    assert "submitted program was not replayed" in skill
    assert "execution outcome may be" in skill
    assert "`HTTP 401` or `HTTP 403`" in skill
    assert "without printing or embedding any credential" in skill
    assert "references/sdk-contract.md" in skill
    assert "references/advanced.md" in skill
    assert "references/patterns.md" in skill
    assert "references/python-recipes.md" in skill
    assert "references/stateful-research.md" in skill
    assert "Split on model judgment" in skill
    assert "exploratory search-only stage is valid" in skill
    assert "A final research result must use `submit`" in skill
    assert "semantic map, not an inner tool-calling agent" in skill
    assert "Use the workspace as program memory" in skill
    assert "program-to-program memory" in skill
    assert "Observations show artifact paths, not their contents" in skill
    assert "Before ending with `NEXT:`" in skill
    assert "no `sdk.workspace` API" in skill
    assert "search.fuse_rrf` -> `content.passages" in skill
    assert "Codex, Claude Code, or another shell-capable agent" in skill.split("---", 2)[1]
    assert "SAC_API_" not in skill
    assert "SAC_CLI_" not in skill
    assert "SAC_AGENT_" not in skill
    assert "CODEX_THREAD_ID" not in skill
    assert "CLAUDE_CODE_SESSION_ID" not in skill
    assert "/v1/sessions" not in skill
    assert "bind_context" not in skill
    assert "SQLite" not in skill
    assert "lease" not in skill


def test_cli_skill_invocation_compiles_and_passes_sandbox_validation() -> None:
    program = _posix_program()
    compile(program, "<search-as-code-cli-invocation>", "exec")
    validate_code(program)
    assert "from opensac_sdk import" in program
    assert "sdk.session.usage()" in program


def test_cli_research_references_stay_in_sync_with_the_mcp_skill() -> None:
    assert (
        CONTRACT_PATH.read_bytes()
        == (MCP_SKILL_DIR / "references" / "sdk-contract.md").read_bytes()
    )
    assert ADVANCED_PATH.read_bytes() == (MCP_SKILL_DIR / "references" / "advanced.md").read_bytes()
    assert PATTERNS_PATH.read_bytes() == (MCP_SKILL_DIR / "references" / "patterns.md").read_bytes()
    assert (
        RECIPES_PATH.read_bytes()
        == (MCP_SKILL_DIR / "references" / "python-recipes.md").read_bytes()
    )
    assert (
        STATEFUL_PATH.read_bytes()
        == (MCP_SKILL_DIR / "references" / "stateful-research.md").read_bytes()
    )

    for path, heading in (
        (PATTERNS_PATH, "## Explore candidates"),
        (PATTERNS_PATH, "## Rank passages across fused candidates"),
        (PATTERNS_PATH, "## Verify selected refs and submit"),
    ):
        program = _fenced_block(path, "python", heading=heading)
        compile(program, "<search-as-code-cli-pattern>", "exec")
        validate_code(program)

    stateful_stages = _fenced_blocks(STATEFUL_PATH, "python")
    assert len(stateful_stages) == 4
    assert max(len(program.splitlines()) for program in stateful_stages) <= 55
    for program in stateful_stages:
        compile(program, "<search-as-code-cli-stateful-stage>", "exec")
        validate_code(program)

    recipes = _fenced_blocks(RECIPES_PATH, "python")
    assert len(recipes) == 4
    for program in recipes:
        compile(program, "<search-as-code-cli-recipe>", "exec")
        validate_code(program)


def test_cli_skill_has_codex_catalog_metadata() -> None:
    metadata = (SKILL_DIR / "agents" / "openai.yaml").read_text(encoding="utf-8")

    assert 'display_name: "Search as Code CLI"' in metadata
    assert 'short_description: "Run grounded OpenSAC research through a local CLI"' in metadata
    assert "$search-as-code-cli" in metadata
