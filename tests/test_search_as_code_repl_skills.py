from __future__ import annotations

from pathlib import Path

from opensac.sandbox.validator import validate_code

ROOT = Path(__file__).parents[1]
BASELINE_DIR = ROOT / ".agents" / "skills" / "search-as-code"
MCP_DIR = ROOT / ".agents" / "skills" / "search-as-code-repl"
CLI_DIR = ROOT / ".agents" / "skills" / "search-as-code-repl-cli"
REFERENCE_NAMES = ("patterns.md", "sdk-contract.md")


def _python_blocks(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    return [part.split("```", 1)[0].strip() for part in text.split("```python")[1:]]


def _python_block(path: Path, heading: str) -> str:
    text = path.read_text(encoding="utf-8").split(heading, 1)[1]
    return text.split("```python", 1)[1].split("```", 1)[0].strip()


def _cli_invocation() -> str:
    skill = (CLI_DIR / "SKILL.md").read_text(encoding="utf-8")
    shell = skill.split("```bash", 1)[1].split("```", 1)[0].strip().splitlines()
    assert shell[0] == "opensac agent-run <<'OPENSAC_PY'"
    assert shell[-1] == "OPENSAC_PY"
    return "\n".join(shell[1:-1])


def test_repl_skills_are_explicit_and_use_separate_adapter_surfaces() -> None:
    mcp_skill = (MCP_DIR / "SKILL.md").read_text(encoding="utf-8")
    cli_skill = (CLI_DIR / "SKILL.md").read_text(encoding="utf-8")
    flat_cli_skill = " ".join(cli_skill.split())

    assert "name: search-as-code-repl" in mcp_skill.split("---", 2)[1]
    assert "$search-as-code-repl" in mcp_skill.split("---", 2)[1]
    assert "sac_run(code)" in mcp_skill
    assert "outer adapter tool, not a Python API" in mcp_skill
    assert "never call `sac_run` from inside that cell" in mcp_skill
    assert "agent-run" not in mcp_skill

    assert "name: search-as-code-repl-cli" in cli_skill.split("---", 2)[1]
    assert "$search-as-code-repl-cli" in cli_skill.split("---", 2)[1]
    assert "opensac agent-run <<'OPENSAC_PY'" in cli_skill
    assert "uv run opensac agent-run" in cli_skill
    assert "outer adapter command" in cli_skill
    assert "Keep the heredoc body as plain Python" in flat_cli_skill
    assert "sac_run(code)" not in cli_skill

    for skill in (mcp_skill, cli_skill):
        flat_skill = " ".join(skill.split())
        assert "execution_mode=persistent_interpreter" in flat_skill
        assert "interpreter_state=ready" in flat_skill
        assert "sdk.capabilities()" in flat_skill
        assert "sdk.workspace" in flat_skill
        assert "sdk.state" not in flat_skill
        assert "sdk.session" not in flat_skill
        assert "sdk.output" not in flat_skill
        assert "not a prescribed workflow" in flat_skill
        assert "No fixed query count, capability sequence, cell split" in flat_skill
        assert "Treat one cell as one semantic checkpoint" in flat_skill
        assert (
            "search -> select a relevant subset -> fetch -> local inspect -> normalize"
            in flat_skill
        )
        assert "sdk.content.passages" not in flat_skill
        assert "Use ordinary Python freely for deterministic orchestration" in flat_skill
        assert "not a required sequence or policy" in flat_skill
        assert "live namespace is the default working memory" in flat_skill
        assert "small cumulative data cache" in flat_skill
        assert "persist an input as `started`" in flat_skill
        assert "persist its `success` or `failure`" in flat_skill
        assert "not a required cell protocol" in flat_skill
        assert "Agent completion is the final response to the user" in flat_skill
        assert "Once printed evidence covers the request" in flat_skill
        assert "starting point rather than a required pipeline" in flat_skill
        assert "Prefer `search.many` ->" not in flat_skill
        assert "never" in flat_skill.lower() and "replay" in flat_skill.lower()
        assert "/v1/sessions" not in flat_skill
        assert "references/patterns.md" in flat_skill
        assert "references/sdk-contract.md" in flat_skill
        assert "references/advanced.md" not in flat_skill
        assert "references/python-recipes.md" not in flat_skill
        assert "references/stateful-research.md" not in flat_skill


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
        baseline_reference = BASELINE_DIR / "references" / name
        assert mcp_reference.is_file()
        assert mcp_reference.read_bytes() == cli_reference.read_bytes()
        assert mcp_reference.read_bytes() == baseline_reference.read_bytes()
    for skill_dir in (MCP_DIR, CLI_DIR):
        for name in ("advanced.md", "python-recipes.md", "stateful-research.md"):
            assert not (skill_dir / "references" / name).exists()

    combined = "\n".join(
        (MCP_DIR / "references" / name).read_text(encoding="utf-8") for name in REFERENCE_NAMES
    )
    assert "sdk.search.many" in combined
    assert "sdk.content.fetch_many" in combined
    assert "sdk.content.passages" not in combined
    assert "llm.complete" not in combined
    assert "sdk.workspace.upsert_jsonl" in combined
    assert "sdk.state" not in combined
    assert "sdk.output" not in combined
    assert "interpreter_lost" in combined
    assert "sdk.session" not in combined

    flat_combined = " ".join(combined.split())
    assert "not a required pipeline" in flat_combined
    assert "requested_source" in flat_combined
    assert "quote is found verbatim in that input" in flat_combined
    assert "fetch-cache.jsonl" in flat_combined
    assert "Applications choose their own artifact layout" in combined


def test_repl_examples_compile_and_pass_sandbox_validation() -> None:
    invocation = _cli_invocation()
    compile(invocation, "<search-as-code-repl-cli-invocation>", "exec")
    validate_code(invocation)

    patterns_path = MCP_DIR / "references" / "patterns.md"
    blocks = _python_blocks(patterns_path)
    assert len(blocks) == 5
    for index, program in enumerate(blocks):
        compile(program, f"<patterns.md:{index}>", "exec")
        validate_code(program)

    composed = _python_block(patterns_path, "## Compose retrieval and focused inspection")
    assert composed.index("sdk.search.many(") < composed.index("sdk.search.fuse_rrf(")
    assert composed.index("sdk.search.fuse_rrf(") < composed.index("sdk.content.fetch_many(")
    assert "sdk.content.passages(" not in composed
    assert "sdk.content.read(" not in composed

    verify = _python_block(patterns_path, "## Verify selected sources and return evidence")
    assert "structured_output_requested" not in verify
    assert "sdk.output" not in verify
    assert "READY: synthesize" in verify

    extraction = _python_block(
        patterns_path,
        "## Optionally extract structured fields from inspected evidence",
    )
    assert "sdk.llm.extract_many(" in extraction
    assert "zip(evidence_items, outcomes, strict=True)" in extraction
    assert "sdk.search." not in extraction

    cache = _python_block(patterns_path, "## Optionally cache selected fetches across calls")
    assert cache.index("sdk.workspace.upsert_jsonl(") < cache.index("sdk.content.fetch_many(")
    assert cache.index("sdk.content.fetch_many(") < cache.rindex("sdk.workspace.upsert_jsonl(")


def test_baseline_skill_catalog_metadata_remains_implicit() -> None:
    for name in ("search-as-code", "search-as-code-cli"):
        metadata = (ROOT / ".agents" / "skills" / name / "agents" / "openai.yaml").read_text(
            encoding="utf-8"
        )
        assert "allow_implicit_invocation" not in metadata
