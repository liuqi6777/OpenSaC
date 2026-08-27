from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from opensac_sdk._resources import StateResource

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
        assert "sdk.session.usage()" in flat_skill
        assert "sdk.output.submit(...)" in flat_skill
        assert "not a prescribed workflow" in flat_skill
        assert "No fixed query count, capability sequence, cell split" in flat_skill
        assert "Treat one cell as one semantic checkpoint" in flat_skill
        assert "search -> fuse -> passages or grep -> focused reads -> normalize" in flat_skill
        assert "live namespace is the default working memory" in flat_skill
        assert "small cumulative cache" in flat_skill
        assert "not a required cell protocol" in flat_skill
        assert "Agent completion is the final response to the user" in flat_skill
        assert "`submit` is optional" in flat_skill
        assert "starting point rather than a required pipeline" in flat_skill
        assert "Prefer `search.many` ->" not in flat_skill
        assert "never" in flat_skill.lower() and "replay" in flat_skill.lower()
        assert "/v1/sessions" not in flat_skill


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

    flat_combined = " ".join(combined.split())
    assert "not a required research pipeline" in flat_combined
    assert "persistent namespace is the default working memory" in flat_combined
    assert "not a workflow state machine" in flat_combined
    assert "`meta.json`" in flat_combined
    assert "`pool.jsonl`" in flat_combined
    assert "`content.jsonl`" in flat_combined
    assert "Do not create per-cell logs" in flat_combined
    assert "namespace shape is application state, not an SDK requirement" in flat_combined
    assert "Persist a constraint fingerprint" not in flat_combined
    assert "Use one task-derived namespace" not in flat_combined


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

    patterns_path = MCP_DIR / "references" / "patterns.md"
    composed = _python_block(patterns_path, "## Compose retrieval and focused inspection")
    assert composed.index("sdk.search.many(") < composed.index("sdk.search.fuse_rrf(")
    assert composed.index("sdk.search.fuse_rrf(") < composed.index("sdk.content.passages(")
    assert composed.index("sdk.content.passages(") < composed.index("sdk.content.read_many(")

    verify = _python_block(patterns_path, "## Verify selected sources and return evidence")
    assert "structured_output_requested = False" in verify

    stateful_blocks = _python_blocks(MCP_DIR / "references" / "stateful-research.md")
    assert len(stateful_blocks) == 2


def test_repl_checkpoint_example_updates_one_cumulative_cache(tmp_path: Path) -> None:
    program = _python_blocks(MCP_DIR / "references" / "stateful-research.md")[0]
    state = StateResource(str(tmp_path))
    sdk = SimpleNamespace(
        state=state,
        session=SimpleNamespace(
            capabilities=lambda: {"mechanisms": {"persistence": True}},
        ),
    )
    module = ModuleType("opensac_sdk")
    module.sdk = sdk
    namespace = {
        "research_goal": "verify the relation",
        "queries": ["first query"],
        "candidate_pool": [
            SimpleNamespace(
                source="doc_1",
                title="First",
                domain="example.test",
                date="1998",
                snippet="first snippet",
                fused_rank=1,
                fused_score=1.0,
            )
        ],
        "evidence_windows": [
            {
                "source": "doc_1",
                "title": "First",
                "text": "first evidence",
                "coordinates": {"start_line": 10, "end_line": 20},
            }
        ],
    }

    with patch.dict(sys.modules, {"opensac_sdk": module}):
        exec(compile(program, "<repl-checkpoint:first>", "exec"), namespace)
        namespace["queries"] = ["first query", "second query"]
        namespace["candidate_pool"].append(
            SimpleNamespace(
                source="doc_2",
                title="Second",
                domain="example.test",
                date="1999",
                snippet="second snippet",
                fused_rank=2,
                fused_score=0.5,
            )
        )
        namespace["evidence_windows"].append(
            {
                "source": "doc_2",
                "title": "Second",
                "text": "second evidence",
                "coordinates": {"start_line": 30, "end_line": 40},
            }
        )
        exec(compile(program, "<repl-checkpoint:second>", "exec"), namespace)

    artifacts = state.list()
    assert {Path(path).name for path in artifacts} == {
        "meta.json",
        "pool.jsonl",
        "content.jsonl",
    }
    meta_path = next(path for path in artifacts if path.endswith("/meta.json"))
    pool_path = next(path for path in artifacts if path.endswith("/pool.jsonl"))
    content_path = next(path for path in artifacts if path.endswith("/content.jsonl"))
    assert state.read_json(meta_path).queries == ["first query", "second query"]
    assert {row.source for row in state.read_jsonl(pool_path)} == {"doc_1", "doc_2"}
    assert {row.source for row in state.read_jsonl(content_path)} == {"doc_1", "doc_2"}


def test_baseline_skill_catalog_metadata_remains_implicit() -> None:
    for name in ("search-as-code", "search-as-code-cli"):
        metadata = (ROOT / ".agents" / "skills" / name / "agents" / "openai.yaml").read_text(
            encoding="utf-8"
        )
        assert "allow_implicit_invocation" not in metadata
