from __future__ import annotations

from pathlib import Path

from opensac.sandbox.validator import validate_code

ROOT = Path(__file__).parents[1]
SKILL_DIR = ROOT / ".agents" / "skills" / "search-as-code-cli"
MCP_SKILL_DIR = ROOT / ".agents" / "skills" / "search-as-code"
SKILL_PATH = SKILL_DIR / "SKILL.md"
CONTRACT_PATH = SKILL_DIR / "references" / "sdk-contract.md"
PATTERNS_PATH = SKILL_DIR / "references" / "patterns.md"
REFERENCE_NAMES = ("patterns.md", "sdk-contract.md")


def _fenced_block(path: Path, fence: str, *, heading: str | None = None) -> str:
    text = path.read_text(encoding="utf-8")
    if heading is not None:
        text = text.split(heading, 1)[1]
    return text.split(f"```{fence}", 1)[1].split("```", 1)[0].strip()


def _posix_program() -> str:
    lines = _fenced_block(SKILL_PATH, "bash").splitlines()
    assert lines[0] == "opensac agent-run <<'OPENSAC_PY'"
    assert lines[-1] == "OPENSAC_PY"
    return "\n".join(lines[1:-1])


def test_cli_skill_is_small_host_neutral_and_routes_details() -> None:
    skill = SKILL_PATH.read_text(encoding="utf-8")
    flat_skill = " ".join(skill.split())
    description = skill.splitlines()[2]

    assert len(skill) < 10_000
    assert "opensac agent-run <<'OPENSAC_PY'" in skill
    assert "uv run opensac agent-run" in skill
    assert "Never expose or override its identity, manage REST sessions" in flat_skill
    assert "state_lost" in skill
    assert "submitted program was not replayed" in skill
    assert "`HTTP 401` or `HTTP 403`" in skill
    assert "without printing or embedding any credential" in flat_skill
    assert "references/sdk-contract.md" in skill
    assert "references/patterns.md" in skill
    assert "references/advanced.md" not in skill
    assert "references/python-recipes.md" not in skill
    assert "references/stateful-research.md" not in skill
    assert "Treat one program as one semantic checkpoint" in flat_skill
    assert "search -> select a relevant subset -> fetch" in flat_skill
    assert "candidates, not a fetch queue" in flat_skill
    assert "Do not fetch the whole result list" in flat_skill
    assert "sdk.content.passages" not in flat_skill
    assert "merely to relocate text already present" in flat_skill
    assert "Use ordinary Python freely for deterministic orchestration" in flat_skill
    assert "not a required sequence or policy" in flat_skill
    assert "across the whole program" in flat_skill
    assert "Make normalized row schemas total" in flat_skill
    assert "small data cache over a workflow state machine" in flat_skill
    assert "Keep each cache cumulative" in flat_skill
    assert "persist an operation as `started`" in flat_skill
    assert "persist each input as `success` or `failure`" in flat_skill
    assert "Agent completion is the final response to the user" in flat_skill
    assert "`submit` is optional" in flat_skill
    assert "same substantive payload could be written before research ran" in flat_skill
    assert "A final research result must use `submit`" not in skill
    assert "program-to-program memory" in skill
    assert "no `sdk.workspace` API" in flat_skill
    assert "shell-capable environments" in description
    assert "Codex" not in description
    assert "Claude" not in description
    assert "Use 2-4 queries" not in skill
    assert "6-12" not in skill
    assert "SAC_API_" not in skill
    assert "SAC_CLI_" not in skill
    assert "SAC_AGENT_" not in skill
    assert "CODEX_THREAD_ID" not in skill
    assert "CLAUDE_CODE_SESSION_ID" not in skill
    assert "/v1/sessions" not in skill
    assert "bind_context" not in skill
    assert "SQLite" not in skill
    assert "lease" not in skill


def test_claude_code_project_uses_cli_instead_of_removed_mcp_binding() -> None:
    assert not (ROOT / ".mcp.json").exists()
    settings_path = ROOT / ".claude" / "settings.json"
    if settings_path.exists():
        settings = settings_path.read_text(encoding="utf-8")
        assert "mcp__opensac__sac_run" not in settings
        assert "bind_context" not in settings


def test_cli_skill_invocation_compiles_and_passes_sandbox_validation() -> None:
    program = _posix_program()
    compile(program, "<search-as-code-cli-invocation>", "exec")
    validate_code(program)
    assert "from opensac_sdk import" in program
    assert "sdk.session.capabilities()" in program
    assert "sdk.session.usage()" not in program


def test_cli_research_references_stay_in_sync_with_the_mcp_skill() -> None:
    for name in REFERENCE_NAMES:
        cli_reference = SKILL_DIR / "references" / name
        mcp_reference = MCP_SKILL_DIR / "references" / name
        assert cli_reference.is_file()
        assert cli_reference.read_bytes() == mcp_reference.read_bytes()
    for name in ("advanced.md", "python-recipes.md", "stateful-research.md"):
        assert not (SKILL_DIR / "references" / name).exists()

    pattern_headings = (
        "## Explore candidates",
        "## Compose retrieval and focused inspection",
        "## Verify selected sources and return evidence",
        "## Optionally extract structured fields from inspected evidence",
        "## Optionally cache selected fetches across calls",
    )
    for heading in pattern_headings:
        path = PATTERNS_PATH
        program = _fenced_block(path, "python", heading=heading)
        compile(program, "<search-as-code-cli-pattern>", "exec")
        validate_code(program)

    composed = _fenced_block(
        PATTERNS_PATH,
        "python",
        heading="## Compose retrieval and focused inspection",
    )
    assert composed.index("sdk.search.many(") < composed.index("sdk.search.fuse_rrf(")
    assert composed.index("sdk.search.fuse_rrf(") < composed.index("sdk.content.fetch_many(")
    assert "sdk.content.passages(" not in composed
    assert "sdk.content.read(" not in composed
    assert "for outcome in fetch_outcomes:" in composed

    verify = _fenced_block(
        PATTERNS_PATH,
        "python",
        heading="## Verify selected sources and return evidence",
    )
    assert "structured_output_requested = False" in verify

    extraction = _fenced_block(
        PATTERNS_PATH,
        "python",
        heading="## Optionally extract structured fields from inspected evidence",
    )
    assert "sdk.llm.extract_many(" in extraction
    assert "zip(evidence_items, outcomes, strict=True)" in extraction
    assert 'quote not in item["text"]' in extraction
    assert "sdk.search." not in extraction
    assert "sdk.output.submit(" not in extraction

    cache = _fenced_block(
        PATTERNS_PATH,
        "python",
        heading="## Optionally cache selected fetches across calls",
    )
    assert cache.index("sdk.state.upsert_jsonl(") < cache.index("sdk.content.fetch_many(")
    assert cache.index("sdk.content.fetch_many(") < cache.rindex("sdk.state.upsert_jsonl(")
    assert "concurrency=" not in cache

    contract = CONTRACT_PATH.read_text(encoding="utf-8")
    assert "sdk.content.passages(" not in contract
    assert "llm.complete" not in contract
    assert "sdk.session.usage" not in contract


def test_cli_skill_has_codex_catalog_metadata() -> None:
    metadata = (SKILL_DIR / "agents" / "openai.yaml").read_text(encoding="utf-8")

    assert 'display_name: "Search as Code CLI"' in metadata
    assert 'short_description: "Run grounded OpenSAC research through a local CLI"' in metadata
    assert "$search-as-code-cli" in metadata
