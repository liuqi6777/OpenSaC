from __future__ import annotations

import contextlib
import io
import re
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from opensac_sdk import BrokerError
from opensac_sdk.models import (
    CapabilityFailure,
    ContentFailure,
    ContentGrepReport,
    ContentMatch,
    ContentSnippet,
    EvidenceLocator,
    EvidenceLocatorError,
    ExtractionError,
    ExtractionResult,
    SearchBatch,
    SearchCandidate,
    SearchHit,
)
from opensac_sdk.search import SearchResource
from opensac_sdk.state import StateResource

from opensac.sandbox.validator import validate_code

SKILL_DIR = Path(__file__).parents[1] / ".agents" / "skills" / "search-as-code"
SKILL_PATH = SKILL_DIR / "SKILL.md"
CONTRACT_PATH = SKILL_DIR / "references" / "sdk-contract.md"
PATTERNS_PATH = SKILL_DIR / "references" / "patterns.md"


def _code_block(path: Path, heading: str) -> str:
    text = path.read_text(encoding="utf-8")
    section = text.split(heading, 1)[1]
    return section.split("```python", 1)[1].split("```", 1)[0]


def _canonical_pattern() -> str:
    return _code_block(PATTERNS_PATH, "## Canonical multi-turn pattern")


def _semantic_pattern() -> str:
    return _code_block(PATTERNS_PATH, "## Checked semantic extraction")


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
    )
    module = ModuleType("opensac_sdk")
    module.BrokerError = BrokerError
    module.sdk = sdk
    printed = io.StringIO()
    source = program or _canonical_pattern()
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
    assert "outcome may be unknown" in skill
    assert "same program blindly" in skill
    assert "OpenSAC locator" in skill
    assert "references/sdk-contract.md" in skill
    assert "references/patterns.md" in skill
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


def test_patterns_compile_and_pass_sandbox_validation() -> None:
    pattern = _canonical_pattern()
    semantic = _semantic_pattern()

    compile(pattern, "<search-as-code-pattern>", "exec")
    compile(semantic, "<search-as-code-semantic-pattern>", "exec")
    validate_code(pattern)
    validate_code(semantic)
    assert 'root = f"runs/{research_id}"' in pattern
    assert "POOL_LIMIT = 200" in pattern
    assert "CONTENT_BATCH = 40" in pattern
    assert "READ_LIMIT_PER_CONSTRAINT = 6" in pattern
    assert "sdk.state.merge_jsonl(pool_path" in pattern
    assert "sdk.state.write_jsonl(pool_path, bounded_pool)" in pattern
    assert '"requirements": {name: spec["requirement"]' in pattern
    assert '"source_policy": source_policy' in pattern
    assert "ordered_refs" in pattern
    assert "attempted[name]" in pattern
    assert "grep_report(list(pool)" not in pattern
    assert "sdk.output.submit(" in pattern


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
    original = _canonical_pattern()
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
    original = _canonical_pattern()
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
