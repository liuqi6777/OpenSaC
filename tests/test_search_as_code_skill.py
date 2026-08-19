from __future__ import annotations

import contextlib
import io
import re
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from opensac_sdk import BrokerError
from opensac_sdk._surface import SDK_SURFACE, SurfaceTier
from opensac_sdk.models import (
    CapabilityFailure,
    ContentFailure,
    ContentGrepReport,
    ContentMatch,
    ContentPassage,
    ContentPassageReport,
    ContentSnippet,
    EvidenceLocator,
    EvidenceLocatorError,
    ExtractionError,
    ExtractionResult,
    PassageCoordinates,
    SearchBatch,
    SearchCandidate,
    SearchHit,
)
from opensac_sdk.search import SearchResource
from opensac_sdk.state import StateResource

from opensac.sandbox.validator import validate_code

ROOT = Path(__file__).parents[1]
SKILL_DIR = ROOT / ".agents" / "skills" / "search-as-code"
SKILL_PATH = SKILL_DIR / "SKILL.md"
CONTRACT_PATH = SKILL_DIR / "references" / "sdk-contract.md"
ADVANCED_PATH = SKILL_DIR / "references" / "advanced.md"
PATTERNS_PATH = SKILL_DIR / "references" / "patterns.md"
RECIPES_PATH = SKILL_DIR / "references" / "python-recipes.md"
STATEFUL_PATH = SKILL_DIR / "references" / "stateful-research.md"
STATEFUL_PROGRAM_PATH = ROOT / "tests" / "data" / "search_as_code_stateful_program.py"


def _code_block(path: Path, heading: str) -> str:
    text = path.read_text(encoding="utf-8")
    section = text.split(heading, 1)[1]
    return section.split("```python", 1)[1].split("```", 1)[0]


def _explore_pattern() -> str:
    return _code_block(PATTERNS_PATH, "## Explore candidates")


def _verify_pattern() -> str:
    return _code_block(PATTERNS_PATH, "## Verify selected refs and submit")


def _rank_pattern() -> str:
    return _code_block(PATTERNS_PATH, "## Rank passages across fused candidates")


def _stateful_pattern() -> str:
    return STATEFUL_PROGRAM_PATH.read_text(encoding="utf-8")


def _workspace_probe() -> str:
    return _python_blocks(STATEFUL_PATH)[2]


def _python_blocks(path: Path) -> list[str]:
    return [part.split("```", 1)[0] for part in path.read_text().split("```python")[1:]]


def _artifact(state: StateResource, name: str) -> str:
    matches = [path for path in state.list() if path.endswith(f"/{name}")]
    assert len(matches) == 1
    return matches[0]


class FakeSearch:
    def __init__(self, *, vary_by_turn: bool = False, hits_per_query: int = 2) -> None:
        self._resource = SearchResource(None)  # type: ignore[arg-type]
        self.vary_by_turn = vary_by_turn
        self.hits_per_query = hits_per_query
        self.turn = 1

    def many(self, queries: list[str], **_kwargs: object) -> list[SearchBatch]:
        prefix = f"turn_{self.turn}_" if self.vary_by_turn else ""
        if self.hits_per_query == 2:
            return [
                SearchBatch(
                    query=query,
                    hits=[
                        SearchHit(
                            ref=f"ref_{prefix}unique_{index}",
                            backend="local",
                            title=f"unique {index}",
                            url=f"https://example.test/{prefix}unique-{index}",
                            domain="example.test",
                            date="1998",
                            snippet=f"unique snippet {index}",
                            rank=1,
                        ),
                        SearchHit(
                            ref=f"ref_{prefix}consensus",
                            backend="local",
                            title="consensus",
                            url=f"https://example.test/{prefix}consensus",
                            domain="example.test",
                            date="1999",
                            snippet="consensus snippet",
                            rank=9,
                        ),
                    ],
                )
                for index, query in enumerate(queries)
            ]

        return [
            SearchBatch(
                query=query,
                hits=[
                    SearchHit(
                        ref=f"ref_{prefix}{query_index}_{hit_index}",
                        backend="local",
                        title=f"result {query_index}-{hit_index}",
                        url=(f"https://example.test/{prefix}{query_index}-{hit_index}"),
                        domain="example.test",
                        date="1998",
                        snippet=f"snippet {query_index}-{hit_index}",
                        rank=hit_index + 1,
                    )
                    for hit_index in range(self.hits_per_query)
                ],
            )
            for query_index, query in enumerate(queries)
        ]

    def fuse_rrf(self, batches: list[SearchBatch], **kwargs: object):
        return self._resource.fuse_rrf(batches, **kwargs)


class FakeContent:
    def __init__(
        self,
        *,
        missing_year: bool = False,
        same_ref: bool = False,
        year_from_turn: int = 1,
        partial_failure: bool = False,
        locator_exhausted: bool = False,
        no_matches: bool = False,
        grep_error: bool = False,
    ) -> None:
        self.missing_year = missing_year
        self.same_ref = same_ref
        self.year_from_turn = year_from_turn
        self.partial_failure = partial_failure
        self.locator_exhausted = locator_exhausted
        self.no_matches = no_matches
        self.grep_error = grep_error
        self.turn = 1
        self.grep_calls: list[tuple[str, tuple[str, ...]]] = []
        self.read_refs: list[str] = []

    @property
    def grep_widths(self) -> list[int]:
        return [len(refs) for _, refs in self.grep_calls]

    def grep_report(self, refs: list[str], pattern: str, **_kwargs: object) -> ContentGrepReport:
        self.grep_calls.append((pattern, tuple(refs)))
        if self.grep_error:
            raise BrokerError(
                "content provider unavailable",
                code="provider_unavailable",
                retryable=True,
                attempts=3,
            )
        is_year = "1998" in pattern
        if self.no_matches or (is_year and (self.missing_year or self.turn < self.year_from_turn)):
            matches = []
        else:
            ref = refs[0] if self.same_ref or not is_year or len(refs) == 1 else refs[1]
            line = 120 if is_year else 12
            matches = [
                ContentMatch(
                    input_index=refs.index(ref),
                    ref=ref,
                    title=ref,
                    line=line,
                    text="1998" if is_year else "target phrase",
                    locator=EvidenceLocator(
                        id=f"grep-{ref}-{line}", ref=ref, kind="selected_passage"
                    ),
                )
            ]
        failures = (
            [
                ContentFailure(
                    input_index=len(refs) - 1,
                    ref=refs[-1],
                    failure=CapabilityFailure(
                        code="provider_timeout",
                        message="document fetch timed out",
                        retryable=True,
                        attempts=3,
                    ),
                )
            ]
            if self.partial_failure
            else []
        )
        return ContentGrepReport(
            matches=matches,
            failures=failures,
            input_count=len(refs),
        )

    def read(self, refs: list[str], **kwargs: object) -> list[ContentSnippet]:
        ref = refs[0]
        self.read_refs.append(ref)
        offset = int(kwargs["offset"])
        text = f"{'1998' if offset > 50 else 'target phrase'} evidence for {ref}"
        if self.locator_exhausted:
            return [
                ContentSnippet(
                    ref=ref,
                    title=ref,
                    text=text,
                    locator_error=EvidenceLocatorError(
                        code="evidence_capacity_exhausted",
                        message="evidence registry is full",
                        retryable=False,
                    ),
                )
            ]
        return [
            ContentSnippet(
                ref=ref,
                title=ref,
                text=text,
                locator=EvidenceLocator(
                    id=f"read-{ref}-{offset}", ref=ref, kind="selected_passage"
                ),
            )
        ]


class FakeOutput:
    def __init__(self) -> None:
        self.submissions: list[tuple[object, list[dict[str, object]]]] = []

    def submit(self, output: object, *, citations: list[dict[str, object]]) -> None:
        self.submissions.append((output, citations))


def _run_pattern(
    tmp_path: Path,
    *,
    program: str | None = None,
    missing_year: bool = False,
    same_ref: bool = False,
    year_from_turn: int = 1,
    turns: int = 1,
    partial_failure: bool = False,
    locator_exhausted: bool = False,
    no_matches: bool = False,
    grep_error: bool = False,
    vary_search_by_turn: bool = False,
    hits_per_query: int = 2,
):
    search = FakeSearch(
        vary_by_turn=vary_search_by_turn,
        hits_per_query=hits_per_query,
    )
    content = FakeContent(
        missing_year=missing_year,
        same_ref=same_ref,
        year_from_turn=year_from_turn,
        partial_failure=partial_failure,
        locator_exhausted=locator_exhausted,
        no_matches=no_matches,
        grep_error=grep_error,
    )
    output = FakeOutput()
    sdk = SimpleNamespace(
        search=search,
        content=content,
        output=output,
        state=StateResource(str(tmp_path)),
        session=SimpleNamespace(
            usage=lambda: {
                "search_calls": 3,
                "content_fetches": 4,
                "terminal_reason": None,
            }
        ),
    )
    module = ModuleType("opensac_sdk")
    module.BrokerError = BrokerError
    module.sdk = sdk
    printed = io.StringIO()
    source = program or _stateful_pattern()
    with patch.dict(sys.modules, {"opensac_sdk": module}), contextlib.redirect_stdout(printed):
        for turn in range(1, turns + 1):
            search.turn = turn
            content.turn = turn
            exec(compile(source, "<search-as-code-pattern>", "exec"), {})
    return sdk, content, output, printed.getvalue()


def _documented_fields(contract: str, model_name: str) -> set[str]:
    match = re.search(
        rf"^- `{re.escape(model_name)}`: (.*?)(?=\n- `|\n\n)",
        contract,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert match is not None
    return set(re.findall(r"`([a-z_]+)`", match.group(1)))


def test_skill_is_small_and_routes_detailed_contracts() -> None:
    skill = SKILL_PATH.read_text(encoding="utf-8")

    assert len(skill) < 6_000
    assert "MCP tool `sac_run(code)`" in skill
    assert "Never create, resume, or delete REST sessions" in skill
    assert "Never call `bind_context`" in skill
    assert "state_lost" in skill
    assert "submitted program was not replayed" in skill
    assert "execution outcome may be" in skill
    assert "same program blindly" in skill
    assert "OpenSAC locator" in skill
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
    assert "fact checking" in skill.split("---", 2)[1]
    assert "session_id" not in skill
    assert "SAC_MCP_" not in skill


def test_skill_has_codex_catalog_metadata() -> None:
    metadata = (SKILL_DIR / "agents" / "openai.yaml").read_text(encoding="utf-8")

    assert 'display_name: "Search as Code"' in metadata
    assert 'short_description: "Research with OpenSAC MCP and trusted citations"' in metadata
    assert "$search-as-code" in metadata


def test_documented_model_fields_match_the_sdk() -> None:
    contract = CONTRACT_PATH.read_text(encoding="utf-8")
    models = {
        "SearchHit": SearchHit,
        "SearchBatch": SearchBatch,
        "SearchCandidate": SearchCandidate,
        "ContentSnippet": ContentSnippet,
        "ContentMatch": ContentMatch,
        "ContentFailure": ContentFailure,
        "PassageCoordinates": PassageCoordinates,
        "ContentPassage": ContentPassage,
        "ContentPassageReport": ContentPassageReport,
        "ContentGrepReport": ContentGrepReport,
        "CapabilityFailure": CapabilityFailure,
        "ExtractionResult": ExtractionResult,
        "ExtractionError": ExtractionError,
        "EvidenceLocator": EvidenceLocator,
        "EvidenceLocatorError": EvidenceLocatorError,
    }

    for name, model in models.items():
        assert _documented_fields(contract, name) == set(model.model_fields)
    assert "GrepFailure" not in contract
    assert "structured interface to the session workspace" in contract
    assert "Execution observations show artifact paths, not their contents" in contract
    assert "not a separate database" in contract
    assert "`sdk.workspace` resource" in contract


def test_surface_tiers_route_exact_signatures_to_the_right_reference() -> None:
    contract = CONTRACT_PATH.read_text(encoding="utf-8")
    advanced = ADVANCED_PATH.read_text(encoding="utf-8")

    for operation in SDK_SURFACE:
        signature = f"{operation.public_name}("
        if operation.tier is SurfaceTier.INTERNAL:
            assert signature not in contract
            assert signature not in advanced
        elif operation.tier is SurfaceTier.ADVANCED:
            assert signature not in contract
            assert signature in advanced
        else:
            assert signature in contract


def test_core_patterns_only_call_core_or_helper_operations() -> None:
    calls = {
        f"sdk.{resource}.{method}" if method else f"sdk.{resource}"
        for resource, method in re.findall(
            r"sdk\.([a-z_]+)(?:\.([a-z_]+))?\(",
            PATTERNS_PATH.read_text(encoding="utf-8"),
        )
    }
    allowed = {
        operation.public_name
        for operation in SDK_SURFACE
        if operation.tier in {SurfaceTier.CORE, SurfaceTier.HELPER}
    }

    assert calls
    assert calls <= allowed


def test_patterns_compile_and_pass_sandbox_validation() -> None:
    explore = _explore_pattern()
    rank = _rank_pattern()
    verify = _verify_pattern()
    stateful = _stateful_pattern()
    stateful_stages = _python_blocks(STATEFUL_PATH)
    workspace_probe = _workspace_probe()
    recipes = _python_blocks(RECIPES_PATH)

    for name, program in (
        ("explore", explore),
        ("rank", rank),
        ("verify", verify),
        ("stateful-fixture", stateful),
        ("workspace-probe", workspace_probe),
        *((f"stateful-stage-{index}", program) for index, program in enumerate(stateful_stages, 1)),
        *((f"recipe-{index}", program) for index, program in enumerate(recipes, 1)),
    ):
        compile(program, f"<search-as-code-{name}-pattern>", "exec")
        validate_code(program)

    assert len(explore.splitlines()) <= 45
    assert "sdk.search.many(" in explore
    assert "sdk.search.fuse_rrf(" in explore
    assert ".candidates[:8]" in explore
    assert "NEXT:" in explore
    assert "sdk.content.grep_report(" not in explore
    assert "sdk.output.submit(" not in explore

    assert "sdk.search.many(" in rank
    assert "sdk.search.fuse_rrf(" in rank
    assert "sdk.content.passages(" in rank
    assert "sdk.output.submit(" not in rank
    assert "NEXT:" in rank

    assert len(verify.splitlines()) <= 75
    assert "sdk.search.many(" not in verify
    assert "NEXT:" in verify
    assert "passage.locator.model_dump" in verify
    assert verify.index("sdk.content.grep_report(") < verify.index("sdk.content.read(")
    assert verify.index("sdk.content.read(") < verify.index("sdk.output.submit(")
    assert verify.count("sdk.output.submit(") == 1

    assert "sdk.state." not in explore
    assert "sdk.state." not in verify

    assert 'root = f"runs/{research_id}"' in stateful
    assert "POOL_LIMIT = 200" in stateful
    assert "CONTENT_BATCH = 40" in stateful
    assert "READ_LIMIT_PER_CONSTRAINT = 6" in stateful
    assert "sdk.state.merge_jsonl(pool_path" in stateful
    assert 'sdk.state.list(f"{root}/")' in stateful
    assert "sdk.state.write_jsonl(pool_path, bounded_pool)" in stateful
    assert '"requirements": {name: spec["requirement"]' in stateful
    assert '"source_policy": source_policy' in stateful
    assert "ordered_refs" in stateful
    assert "attempted[name]" in stateful
    assert "grep_report(list(pool)" not in stateful
    assert "sdk.output.submit(" in stateful

    stateful_reference = STATEFUL_PATH.read_text(encoding="utf-8")
    assert len(stateful_reference.splitlines()) <= 225
    assert len(stateful_stages) == 4
    assert max(len(program.splitlines()) for program in stateful_stages) <= 55
    assert "code block is one stage" in stateful_reference
    assert "Searching first" in stateful_reference
    assert "## Canonical stateful pattern" not in stateful_reference
    assert "## Workspace contract" in stateful_reference
    assert "| `manifest.json` |" in stateful_reference
    assert "| `pool.jsonl` |" in stateful_reference
    assert "| `evidence.json` |" in stateful_reference
    assert "| `attempts.json` |" in stateful_reference
    assert "workspace is the program's durable notebook" in stateful_reference

    assert len(recipes) == 4
    recipe_text = "\n".join(recipes)
    assert "for year in years" in recipe_text
    assert "list(filter(keep, candidates))" in recipe_text
    assert "sdk.llm.extract_many(" in recipe_text
    assert 'quote in item["text"]' in recipe_text
    assert "sdk.search.many(followup_queries" in recipe_text
    assert "MAX_FOLLOWUPS = 6" in recipe_text
    assert "while " not in recipe_text


def test_query_recipe_builds_a_bounded_unique_year_matrix() -> None:
    namespace: dict[str, object] = {}
    exec(compile(_python_blocks(RECIPES_PATH)[0], "<query-grid-recipe>", "exec"), namespace)

    queries = namespace["queries"]
    assert isinstance(queries, list)
    assert len(queries) == 20
    assert len(set(queries)) == len(queries)
    assert all(any(str(year) in query for query in queries) for year in range(2019, 2024))


def test_workspace_probe_recovers_saved_research_progress(tmp_path: Path) -> None:
    research_id = "workspace-test"
    root = f"runs/{research_id}"
    state = StateResource(str(tmp_path))
    state.write_json(f"{root}/manifest.json", {"task": "test"})
    state.write_jsonl(f"{root}/pool.jsonl", [{"ref": "ref_1"}])
    state.write_json(f"{root}/evidence.json", {"phrase": {"ref": "ref_1"}})
    state.write_json(f"{root}/attempts.json", {"phrase": {"refs": ["ref_1"]}})

    program = _workspace_probe().replace("copy-the-task-derived-id", research_id)
    _, _, output, printed = _run_pattern(tmp_path, program=program)

    assert f"WORKSPACE research={research_id}" in printed
    assert "pool=1" in printed
    assert "evidence=['phrase']" in printed
    assert "terminal=None" in printed
    assert printed.strip().endswith("NEXT: resume only the missing constraint or stage")
    assert not output.submissions


def test_stateful_stage_examples_progress_from_workspace_to_submit(tmp_path: Path) -> None:
    stages = _python_blocks(STATEFUL_PATH)

    sdk, _, output, searched = _run_pattern(tmp_path, program=stages[0])
    match = re.search(r"research=([0-9a-f]{12})", searched)
    assert match is not None
    research_id = match.group(1)
    assert sdk.state.read_jsonl(_artifact(sdk.state, "pool.jsonl"))
    assert "NEXT: inspect candidates" in searched
    assert not output.submissions

    _, _, output, verified_phrase = _run_pattern(
        tmp_path,
        program=stages[1].replace("copy-the-task-derived-id", research_id),
    )
    assert "constraint=phrase verified=True" in verified_phrase
    assert not output.submissions

    year_stage = (
        stages[1]
        .replace("copy-the-task-derived-id", research_id)
        .replace('name = "phrase"', 'name = "year"')
        .replace(
            'requirement = "Attribute the target phrase to the entity."',
            'requirement = "Relate the target event to 1998 or 1999."',
        )
        .replace(
            'pattern = r"(target phrase|other spelling)"',
            r'pattern = r"\b(1998|1999)\b"',
        )
    )
    sdk, _, output, verified_year = _run_pattern(tmp_path, program=year_stage)
    assert "constraint=year verified=True" in verified_year
    assert set(sdk.state.read_json(_artifact(sdk.state, "evidence.json"))) == {"phrase", "year"}
    assert not output.submissions

    _, _, output, printed = _run_pattern(
        tmp_path,
        program=stages[3].replace("copy-the-task-derived-id", research_id),
    )
    assert printed == ""
    assert len(output.submissions) == 1


def test_explore_pattern_stops_for_model_judgment(tmp_path: Path) -> None:
    _, content, output, printed = _run_pattern(tmp_path, program=_explore_pattern())

    lines = printed.strip().splitlines()
    assert 1 <= sum(line.startswith("CANDIDATE ") for line in lines) <= 8
    assert lines[-1].startswith("NEXT: inspect")
    assert not content.grep_calls
    assert not output.submissions


def test_verify_pattern_submits_instead_of_printing_final_evidence(tmp_path: Path) -> None:
    _, content, output, printed = _run_pattern(tmp_path, program=_verify_pattern())

    assert printed == ""
    assert len(content.grep_calls) == 2
    assert content.read_refs
    assert len(output.submissions) == 1
    submitted, citations = output.submissions[0]
    assert {row["constraint"] for row in submitted["evidence"]} == {"phrase", "year"}
    assert all(citation["locator"]["id"].startswith("read-") for citation in citations)


def test_verify_pattern_ends_in_next_when_model_judgment_is_needed(tmp_path: Path) -> None:
    _, _, output, printed = _run_pattern(
        tmp_path,
        program=_verify_pattern(),
        missing_year=True,
    )

    lines = printed.strip().splitlines()
    assert lines[0].startswith("EVIDENCE phrase:")
    assert lines[-1].startswith("NEXT:")
    assert "missing=['year']" in lines[-1]
    assert not output.submissions


def test_pattern_keeps_one_ranked_pool_and_cites_read_passages(tmp_path: Path) -> None:
    sdk, content, output, printed = _run_pattern(tmp_path)

    pool = sdk.state.read_jsonl(_artifact(sdk.state, "pool.jsonl"))
    assert len(pool) == 4
    assert pool[0].ref == "ref_consensus"
    assert set(pool[0]) == {
        "ref",
        "title",
        "url",
        "domain",
        "date",
        "snippet",
        "score",
    }
    assert content.grep_widths == [4, 4]
    assert "pool=4" in printed
    assert "ref_" not in printed

    evidence = sdk.state.read_json(_artifact(sdk.state, "evidence.json"))
    assert set(evidence) == {"phrase", "year"}
    assert len(output.submissions) == 1
    _, citations = output.submissions[0]
    assert all(
        citation["ref"] == citation["locator"]["ref"]
        and citation["locator"]["id"].startswith("read-")
        for citation in citations
    )


def test_pattern_pool_score_is_idempotent_across_replayed_stages(tmp_path: Path) -> None:
    sdk, _, _, _ = _run_pattern(tmp_path)
    pool_path = _artifact(sdk.state, "pool.jsonl")
    first = {row.ref: row.score for row in sdk.state.read_jsonl(pool_path)}

    sdk, _, _, _ = _run_pattern(tmp_path, turns=2)
    replayed = {
        row.ref: row.score for row in sdk.state.read_jsonl(_artifact(sdk.state, "pool.jsonl"))
    }

    assert replayed == first


def test_pattern_does_not_submit_with_an_unsupported_constraint(tmp_path: Path) -> None:
    sdk, _, output, printed = _run_pattern(tmp_path, missing_year=True)

    assert "unsupported: ['year']" in printed
    assert not output.submissions
    evidence = sdk.state.read_json(_artifact(sdk.state, "evidence.json"))
    assert set(evidence) == {"phrase"}
    attempts = sdk.state.read_json(_artifact(sdk.state, "attempts.json"))
    assert attempts.year.fingerprint
    assert attempts.year.refs


def test_pattern_verifies_far_apart_constraints_in_the_same_document(tmp_path: Path) -> None:
    sdk, _, output, _ = _run_pattern(tmp_path, same_ref=True)

    evidence = sdk.state.read_json(_artifact(sdk.state, "evidence.json"))
    assert {row.ref for row in evidence.values()} == {"ref_consensus"}
    assert all(
        set(row) == {"fingerprint", "requirement", "ref", "text", "locator"}
        for row in evidence.values()
    )
    assert len(output.submissions) == 1
    _, citations = output.submissions[0]
    assert len({citation["locator"]["id"] for citation in citations}) == 2


def test_pattern_unions_new_evidence_across_turns(tmp_path: Path) -> None:
    sdk, _, output, printed = _run_pattern(
        tmp_path,
        year_from_turn=2,
        turns=2,
        vary_search_by_turn=True,
    )

    assert "unsupported: ['year']" in printed
    assert len(output.submissions) == 1
    submitted, citations = output.submissions[0]
    assert {row["constraint"] for row in submitted["evidence"]} == {"phrase", "year"}
    assert len(citations) == 2
    evidence = sdk.state.read_json(_artifact(sdk.state, "evidence.json"))
    assert set(evidence) == {"phrase", "year"}
    assert evidence.phrase.ref.startswith("ref_turn_1_")
    assert evidence.year.ref.startswith("ref_turn_2_")


def test_pattern_reports_partial_fetch_failure_and_keeps_matches(tmp_path: Path) -> None:
    _, _, output, printed = _run_pattern(tmp_path, partial_failure=True)

    assert "code=provider_timeout" in printed
    assert len(output.submissions) == 1


def test_pattern_never_cites_locator_capacity_exhausted_text(tmp_path: Path) -> None:
    sdk, _, output, printed = _run_pattern(tmp_path, locator_exhausted=True)

    assert "code=evidence_capacity_exhausted" in printed
    assert "unsupported: ['phrase', 'year']" in printed
    assert not output.submissions
    assert not any(path.endswith("/evidence.json") for path in sdk.state.list())


def test_pattern_bounds_pool_and_content_batches(tmp_path: Path) -> None:
    sdk, content, output, _ = _run_pattern(
        tmp_path,
        turns=9,
        no_matches=True,
        vary_search_by_turn=True,
        hits_per_query=10,
    )

    pool = sdk.state.read_jsonl(_artifact(sdk.state, "pool.jsonl"))
    assert len(pool) == 200
    assert content.grep_widths
    assert max(content.grep_widths) <= 40
    assert not output.submissions


def test_pattern_does_not_rescan_attempted_refs(tmp_path: Path) -> None:
    _, content, output, printed = _run_pattern(
        tmp_path,
        turns=2,
        missing_year=True,
    )

    year_calls = [refs for pattern, refs in content.grep_calls if "1998" in pattern]
    assert len(year_calls) == 1
    assert "year: no untried candidates; change the queries" in printed
    assert not output.submissions


def test_pattern_does_not_replay_refs_after_call_wide_content_failure(tmp_path: Path) -> None:
    _, content, output, printed = _run_pattern(
        tmp_path,
        turns=2,
        grep_error=True,
    )

    assert len(content.grep_calls) == 2
    assert printed.count("code=provider_unavailable") == 2
    assert printed.count("no untried candidates; change the queries") == 2
    assert not output.submissions


def test_pattern_isolates_a_changed_task_in_a_new_state_namespace(tmp_path: Path) -> None:
    original = _stateful_pattern()
    changed = original.replace(
        "Identify the target entity and verify the requested phrase and year.",
        "Identify a different entity while verifying the same phrase and year.",
    )
    assert changed != original

    _run_pattern(tmp_path, program=original)
    sdk, _, _, _ = _run_pattern(tmp_path, program=changed)

    pools = [path for path in sdk.state.list() if path.endswith("/pool.jsonl")]
    manifests = [path for path in sdk.state.list() if path.endswith("/manifest.json")]
    assert len(pools) == 2
    assert len(manifests) == 2


def test_pattern_revalidates_changed_regex_in_the_same_namespace(tmp_path: Path) -> None:
    original = _stateful_pattern()
    changed = original.replace(
        r"(target phrase|other spelling)",
        r"target phrase",
    )
    assert changed != original

    _run_pattern(tmp_path, program=original)
    sdk, content, output, _ = _run_pattern(tmp_path, program=changed)

    assert len([path for path in sdk.state.list() if path.endswith("/pool.jsonl")]) == 1
    assert len([path for path in sdk.state.list() if path.endswith("/manifest.json")]) == 1
    assert any(pattern == "target phrase" for pattern, _ in content.grep_calls)
    evidence = sdk.state.read_json(_artifact(sdk.state, "evidence.json"))
    assert set(evidence) == {"phrase", "year"}
    assert len(output.submissions) == 1
