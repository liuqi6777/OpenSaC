from __future__ import annotations

import contextlib
import io
import re
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from opensac_sdk import BrokerError
from opensac_sdk._record import Record, record
from opensac_sdk._resources import SearchResource, StateResource
from opensac_sdk._surface import SDK_SURFACE, SurfaceTier

from opensac.backends.search import SearchBatch, SearchHit
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
    return _code_block(PATTERNS_PATH, "## Verify selected sources and return evidence")


def _rank_pattern() -> str:
    return _code_block(PATTERNS_PATH, "## Compose retrieval and focused inspection")


def _stateful_pattern() -> str:
    return STATEFUL_PROGRAM_PATH.read_text(encoding="utf-8")


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
        self.many_calls: list[tuple[str, ...]] = []

    def many(self, queries: list[str], **_kwargs: object) -> list[Record]:
        self.many_calls.append(tuple(queries))
        prefix = f"turn_{self.turn}_" if self.vary_by_turn else ""
        if self.hits_per_query == 2:
            batches = [
                SearchBatch(
                    query=query,
                    hits=[
                        SearchHit(
                            source=f"doc_{prefix}unique_{index}",
                            backend="local",
                            title=f"unique {index}",
                            url=f"https://example.test/{prefix}unique-{index}",
                            domain="example.test",
                            date="1998",
                            snippet=f"unique snippet {index}",
                            rank=1,
                        ),
                        SearchHit(
                            source=f"doc_{prefix}consensus",
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
                record(
                    {
                        "query": batch.query,
                        "status": "success",
                        "hits": batch.model_dump(mode="json")["hits"],
                    }
                )
                for batch in batches
            ]

        batches = [
            SearchBatch(
                query=query,
                hits=[
                    SearchHit(
                        source=f"doc_{prefix}{query_index}_{hit_index}",
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
        return [
            record(
                {
                    "query": batch.query,
                    "status": "success",
                    "hits": batch.model_dump(mode="json")["hits"],
                }
            )
            for batch in batches
        ]

    def fuse_rrf(self, report: list[Record], **kwargs: object):
        return self._resource.fuse_rrf(report, **kwargs)


class FakeContent:
    def __init__(
        self,
        *,
        missing_year: bool = False,
        same_source: bool = False,
        year_from_turn: int = 1,
        partial_failure: bool = False,
        no_matches: bool = False,
        grep_error: bool = False,
    ) -> None:
        self.missing_year = missing_year
        self.same_source = same_source
        self.year_from_turn = year_from_turn
        self.partial_failure = partial_failure
        self.no_matches = no_matches
        self.grep_error = grep_error
        self.turn = 1
        self.grep_calls: list[tuple[str, tuple[str, ...]]] = []
        self.passage_calls: list[tuple[str, tuple[str, ...]]] = []
        self.read_sources: list[str] = []

    @property
    def grep_widths(self) -> list[int]:
        return [len(sources) for _, sources in self.grep_calls]

    def grep(
        self,
        pattern: str,
        *,
        sources: list[str],
        **_kwargs: object,
    ) -> list[Record]:
        self.grep_calls.append((pattern, tuple(sources)))
        if self.grep_error:
            raise BrokerError(
                "content provider unavailable",
                code="provider_unavailable",
                retryable=True,
                attempts=3,
            )
        is_year = "1998" in pattern
        if self.no_matches or (is_year and (self.missing_year or self.turn < self.year_from_turn)):
            matched_index = None
            match = None
        else:
            source = (
                sources[0] if self.same_source or not is_year or len(sources) == 1 else sources[1]
            )
            line = 120 if is_year else 12
            matched_index = sources.index(source)
            match = record(
                {
                    "line": line,
                    "text": "1998" if is_year else "target phrase",
                    "before": [],
                    "after": [],
                    "spans": [
                        {
                            "start_character": 0,
                            "end_character": 4 if is_year else 6,
                        }
                    ],
                }
            )
        failed_index = len(sources) - 1 if self.partial_failure else None
        outcomes = []
        for index, source in enumerate(sources):
            if index == failed_index:
                outcomes.append(
                    record(
                        {
                            "source": source,
                            "title": None,
                            "status": (
                                "failure[provider_timeout]: document fetch timed out; "
                                "retryable=true; attempts=3"
                            ),
                            "matches": [],
                            "next_start_line": None,
                        }
                    )
                )
                continue
            outcomes.append(
                record(
                    {
                        "source": source,
                        "title": source,
                        "status": "success",
                        "matches": [match] if index == matched_index and match is not None else [],
                        "next_start_line": None,
                    }
                )
            )
        return outcomes

    def read(self, source: str, **kwargs: object) -> Record:
        self.read_sources.append(source)
        start_line = int(kwargs.get("start_line", 1))
        line_count = int(kwargs.get("line_count", 200))
        text = f"{'1998' if start_line > 50 else 'target phrase'} evidence for {source}"
        return record(
            {
                "source": source,
                "title": source,
                "date": "1998",
                "text": text,
                "metadata": {},
                "window": {
                    "start_line": start_line,
                    "start_character": int(kwargs.get("start_character", 0)),
                    "end_line": start_line + line_count - 1,
                    "end_character": len(text),
                    "total_lines": 200,
                    "next": None,
                    "truncated_by_max_chars": False,
                },
            }
        )

    def passages(
        self,
        query: str,
        *,
        sources: list[str],
        limit: int = 10,
        limit_per_source: int = 2,
        **_kwargs: object,
    ) -> Record:
        self.passage_calls.append((query, tuple(sources)))
        rows = []
        bounded_sources = sources if limit_per_source > 0 else []
        for index, source in enumerate(bounded_sources):
            if len(rows) >= limit:
                break
            text = f"target phrase 1998 evidence for {source}"
            rows.append(
                {
                    "source": source,
                    "title": source,
                    "date": "1998",
                    "text": text,
                    "coordinates": {
                        "start_line": 10 + index,
                        "start_character": 0,
                        "end_line": 10 + index,
                        "end_character": len(text),
                    },
                    "rank": index + 1,
                    "score": 1.0 / (index + 1),
                    "ranker": "lexical:bm25",
                }
            )
        return record(
            {
                "query": query,
                "passages": rows,
                "failures": [],
                "warnings": [],
                "input_count": len(sources),
                "unique_source_count": len(set(sources)),
            }
        )


class FakeOutput:
    def __init__(self) -> None:
        self.submissions: list[tuple[object, list[str]]] = []

    def submit(self, value: object, *, citations: list[str]) -> None:
        self.submissions.append((value, citations))


def _run_pattern(
    tmp_path: Path,
    *,
    program: str | None = None,
    missing_year: bool = False,
    same_source: bool = False,
    year_from_turn: int = 1,
    turns: int = 1,
    partial_failure: bool = False,
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
        same_source=same_source,
        year_from_turn=year_from_turn,
        partial_failure=partial_failure,
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


def test_skill_teaches_contracts_without_prescribing_research_strategy() -> None:
    skill = SKILL_PATH.read_text(encoding="utf-8")
    flat_skill = " ".join(skill.split())
    description = skill.splitlines()[2]

    assert len(skill) < 10_000
    assert "Codex" not in description
    assert "Claude" not in description
    assert "MCP tool `sac_run(code)`" in flat_skill
    assert "REST sessions" not in flat_skill
    assert "request metadata" not in flat_skill
    assert "Read usage or deployment capabilities with `sdk.session`" in flat_skill
    assert "state_lost" in flat_skill
    assert "submitted program was not replayed" in flat_skill
    assert "execution outcome may be" in flat_skill
    assert "same program blindly" in flat_skill
    assert "Public web URLs" in flat_skill
    assert "references/sdk-contract.md" in flat_skill
    assert "references/advanced.md" in flat_skill
    assert "references/patterns.md" in flat_skill
    assert "references/python-recipes.md" in flat_skill
    assert "references/stateful-research.md" in flat_skill
    assert "Choose the strategy yourself" in flat_skill
    assert "teaches how to encode it as OpenSAC code" in flat_skill
    assert "No fixed query count, capability" in flat_skill
    assert "stage split, or workspace schema is required" in flat_skill
    assert "Issue another search batch only" not in flat_skill
    assert "Once useful authoritative candidates exist" not in flat_skill
    assert "Agent completion is the final response to the user" in flat_skill
    assert "`submit` is optional" in flat_skill
    assert "Material claims, evidence, status, and citations" in flat_skill
    assert "Prefer a small data cache over a workflow state machine" in flat_skill
    assert "filter repeated queries or sources" in flat_skill
    assert "Keep each cache cumulative" in flat_skill
    assert "the same pool and content artifacts" in flat_skill
    assert "Do not print raw result lists, full passages, or the ledger" in flat_skill
    assert "Runtime metrics alone" in flat_skill
    assert "A final research result must use `submit`" not in flat_skill
    assert "not as new evidence" in flat_skill
    assert "program-to-program memory" in flat_skill
    assert "observations show artifact paths, not their" in flat_skill
    assert "no `sdk.workspace` API" in flat_skill
    assert "sdk.content.passages" in flat_skill
    assert "sdk.content.grep" in flat_skill
    assert "sdk.content.read" in flat_skill
    assert "Treat every example as a starting point rather than a required pipeline" in flat_skill
    assert "fact checking" in skill.split("---", 2)[1]
    assert "Use 2-4 queries" not in flat_skill
    assert "6-12" not in flat_skill
    assert "Before ending with `NEXT:`" not in flat_skill
    assert "session_id" not in flat_skill
    assert "SAC_MCP_" not in flat_skill


def test_example_references_are_explicitly_non_prescriptive() -> None:
    patterns = PATTERNS_PATH.read_text(encoding="utf-8")
    stateful = STATEFUL_PATH.read_text(encoding="utf-8")
    flat_patterns = " ".join(patterns.split())
    flat_stateful = " ".join(stateful.split())

    assert "not a required pipeline" in flat_patterns
    assert "query count, bounds, call grouping, and stopping point are examples" in flat_patterns
    assert "Multiple `sac_run` calls alone do not require state" in flat_stateful
    assert "Adapt its inputs and bounds; they are not a required strategy" in flat_stateful


def test_skill_has_codex_catalog_metadata() -> None:
    metadata = (SKILL_DIR / "agents" / "openai.yaml").read_text(encoding="utf-8")

    assert 'display_name: "Search as Code"' in metadata
    assert 'short_description: "Research with OpenSAC MCP and source URLs"' in metadata
    assert "$search-as-code" in metadata


def test_contract_documents_records_without_a_public_model_hierarchy() -> None:
    contract = CONTRACT_PATH.read_text(encoding="utf-8")

    assert "opensac_sdk.types" not in contract
    assert "There is no public SDK model hierarchy" in contract
    assert "Mapping access is canonical" in contract
    assert "known non-colliding fields" in contract
    assert "Fused candidate" in contract
    assert "Passage report" in contract
    assert "structured session-workspace interface" in contract
    assert "there is no `sdk.workspace` resource" in contract
    assert "Adapter failures occur outside the sandbox" in contract
    assert "0-based, end-exclusive" in contract
    assert "`read.window.next`" in contract
    assert "Search outcome list" in contract
    assert "Grep outcome list" in contract
    assert 'Only compare it with `"success"`; do not parse failure text' in contract


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
    recipes = _python_blocks(RECIPES_PATH)

    for name, program in (
        ("explore", explore),
        ("rank", rank),
        ("verify", verify),
        ("stateful-fixture", stateful),
        *((f"stateful-cache-{index}", program) for index, program in enumerate(stateful_stages, 1)),
        *((f"recipe-{index}", program) for index, program in enumerate(recipes, 1)),
    ):
        compile(program, f"<search-as-code-{name}-pattern>", "exec")
        validate_code(program)

    assert len(explore.splitlines()) <= 45
    assert "sdk.search.many(" in explore
    assert "sdk.search.fuse_rrf(" in explore
    assert "fuse_rrf(outcomes, k=60)[:8]" in explore
    assert "NEXT:" in explore
    assert "sdk.content.grep(" not in explore
    assert "sdk.output.submit(" not in explore

    assert "sdk.search.many(" in rank
    assert "sdk.search.fuse_rrf(" in rank
    assert "sdk.content.passages(" in rank
    assert "sdk.content.read(" in rank
    assert "for window in windows:" in rank
    assert "sdk.output.submit(" not in rank
    assert "NEXT:" in rank
    assert "for window, item in read_results[:4]:" in rank
    assert "[:600]" in rank

    assert len(verify.splitlines()) <= 90
    assert "sdk.search.many(" not in verify
    assert "NEXT:" in verify
    assert '"source": passage.source' in verify
    assert verify.index("sdk.content.grep(") < verify.index("sdk.content.read(")
    assert verify.index("sdk.content.read(") < verify.index("sdk.output.submit(")
    assert verify.count("sdk.output.submit(") == 1
    assert "structured_output_requested = False" in verify
    assert "NEXT: synthesize the user-facing answer" in verify

    assert "sdk.state." not in explore
    assert "sdk.state." not in verify

    assert 'root = f"runs/{research_id}"' in stateful
    assert "POOL_LIMIT = 200" in stateful
    assert "CONTENT_BATCH = 40" in stateful
    assert "READ_LIMIT_PER_CONSTRAINT = 6" in stateful
    assert "sdk.state.upsert_jsonl(pool_path" in stateful
    assert 'sdk.state.list(f"{root}/")' in stateful
    assert "sdk.state.write_jsonl(pool_path, bounded_pool)" in stateful
    assert '"requirements": {name: spec["requirement"]' in stateful
    assert '"source_policy": source_policy' in stateful
    assert "ordered_sources" in stateful
    assert "attempted[name]" in stateful
    assert "grep(list(pool)" not in stateful
    assert "sdk.output.submit(" in stateful

    stateful_reference = STATEFUL_PATH.read_text(encoding="utf-8")
    assert len(stateful_reference.splitlines()) <= 230
    assert len(stateful_stages) == 1
    assert "Multiple `sac_run` calls alone do not require state" in stateful_reference
    assert "Adapt its inputs and bounds; they are not a required strategy" in stateful_reference
    assert "## Canonical stateful pattern" not in stateful_reference
    assert "## Small data model" in stateful_reference
    assert "| `meta.json` |" in stateful_reference
    assert "| `pool.jsonl` |" in stateful_reference
    assert "| `content.jsonl` |" in stateful_reference
    assert "not a workflow state machine" in stateful_reference
    assert "Keep one cumulative file for each role" in stateful_reference
    assert "do not create `pool_round2.jsonl` or `content_stage3.jsonl`" in stateful_reference
    assert "sdk.state.write_jsonl(pool_path, pool)" in stateful_reference
    assert "sdk.state.write_jsonl(content_path, content)" in stateful_reference

    assert len(recipes) == 4
    recipe_text = "\n".join(recipes)
    assert "for year in years" in recipe_text
    assert "list(filter(keep, candidates))" in recipe_text
    assert "sdk.llm.extract(" in recipe_text
    assert "for passage, item in zip(" in recipe_text
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


def test_stateful_cache_example_reuses_cumulative_artifacts(tmp_path: Path) -> None:
    program = _python_blocks(STATEFUL_PATH)[0]

    sdk, content, output, first = _run_pattern(tmp_path, program=program)

    assert sdk.search.many_calls
    assert content.passage_calls
    assert content.read_sources
    assert "new_queries=2" in first
    assert "EVIDENCE source=" in first
    assert first.strip().endswith(
        "NEXT: judge these rows, answer if complete, or extend unresolved requirements"
    )
    artifact_names = {Path(path).name for path in sdk.state.list()}
    assert artifact_names == {"meta.json", "pool.jsonl", "content.jsonl"}
    meta = sdk.state.read_json(_artifact(sdk.state, "meta.json"))
    pool = sdk.state.read_jsonl(_artifact(sdk.state, "pool.jsonl"))
    cached_content = sdk.state.read_jsonl(_artifact(sdk.state, "content.jsonl"))
    assert len(meta.queries) == 2
    assert pool
    assert cached_content
    assert all(row.key.startswith(f"{row.source}#L") for row in cached_content)
    assert not output.submissions

    resumed_sdk, resumed_content, resumed_output, second = _run_pattern(
        tmp_path,
        program=program,
    )

    assert not resumed_sdk.search.many_calls
    assert not resumed_content.passage_calls
    assert not resumed_content.read_sources
    assert "new_queries=0" in second
    assert {Path(path).name for path in resumed_sdk.state.list()} == artifact_names
    assert not resumed_output.submissions


def test_explore_pattern_stops_for_model_judgment(tmp_path: Path) -> None:
    _, content, output, printed = _run_pattern(tmp_path, program=_explore_pattern())

    lines = printed.strip().splitlines()
    assert 1 <= sum(line.startswith("CANDIDATE ") for line in lines) <= 8
    assert lines[-1].startswith("NEXT: inspect")
    assert not content.grep_calls
    assert not output.submissions


def test_verify_pattern_returns_runtime_evidence_for_model_synthesis(tmp_path: Path) -> None:
    _, content, output, printed = _run_pattern(tmp_path, program=_verify_pattern())

    assert len(content.grep_calls) == 2
    assert content.read_sources
    assert "EVIDENCE phrase:" in printed
    assert "EVIDENCE year:" in printed
    assert printed.strip().endswith(
        "NEXT: synthesize the user-facing answer from this verified evidence"
    )
    assert not output.submissions


def test_verify_pattern_submits_runtime_evidence_when_requested(tmp_path: Path) -> None:
    program = _verify_pattern().replace(
        "structured_output_requested = False",
        "structured_output_requested = True",
    )
    _, content, output, printed = _run_pattern(tmp_path, program=program)

    assert printed == ""
    assert len(content.grep_calls) == 2
    assert content.read_sources
    assert len(output.submissions) == 1
    submitted, citations = output.submissions[0]
    assert {row["constraint"] for row in submitted["evidence"]} == {"phrase", "year"}
    assert citations
    assert all(isinstance(citation, str) for citation in citations)


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
    assert pool[0].source == "doc_consensus"
    assert set(pool[0]) == {
        "source",
        "title",
        "domain",
        "date",
        "snippet",
        "score",
    }
    assert content.grep_widths == [4, 4]
    assert "pool=4" in printed
    assert "doc_" not in printed

    evidence = sdk.state.read_json(_artifact(sdk.state, "evidence.json"))
    assert set(evidence) == {"phrase", "year"}
    assert len(output.submissions) == 1
    _, citations = output.submissions[0]
    assert citations == list(dict.fromkeys(row.source for row in evidence.values()))


def test_pattern_pool_score_is_idempotent_across_replayed_stages(tmp_path: Path) -> None:
    sdk, _, _, _ = _run_pattern(tmp_path)
    pool_path = _artifact(sdk.state, "pool.jsonl")
    first = {row.source: row.score for row in sdk.state.read_jsonl(pool_path)}

    sdk, _, _, _ = _run_pattern(tmp_path, turns=2)
    replayed = {
        row.source: row.score for row in sdk.state.read_jsonl(_artifact(sdk.state, "pool.jsonl"))
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
    assert attempts.year.sources


def test_pattern_verifies_far_apart_constraints_in_the_same_document(tmp_path: Path) -> None:
    sdk, _, output, _ = _run_pattern(tmp_path, same_source=True)

    evidence = sdk.state.read_json(_artifact(sdk.state, "evidence.json"))
    assert {row.source for row in evidence.values()} == {"doc_consensus"}
    assert all(
        set(row) == {"fingerprint", "requirement", "source", "text"} for row in evidence.values()
    )
    assert len(output.submissions) == 1
    _, citations = output.submissions[0]
    assert citations == ["doc_consensus"]


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
    assert evidence.phrase.source.startswith("doc_turn_1_")
    assert evidence.year.source.startswith("doc_turn_2_")


def test_pattern_reports_partial_fetch_failure_and_keeps_matches(tmp_path: Path) -> None:
    _, _, output, printed = _run_pattern(tmp_path, partial_failure=True)

    assert "failure[provider_timeout]" in printed
    assert len(output.submissions) == 1


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


def test_pattern_does_not_rescan_attempted_sources(tmp_path: Path) -> None:
    _, content, output, printed = _run_pattern(
        tmp_path,
        turns=2,
        missing_year=True,
    )

    year_calls = [sources for pattern, sources in content.grep_calls if "1998" in pattern]
    assert len(year_calls) == 1
    assert "year: no untried candidates; change the queries" in printed
    assert not output.submissions


def test_pattern_does_not_replay_sources_after_call_wide_content_failure(tmp_path: Path) -> None:
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
