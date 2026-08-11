from __future__ import annotations

import contextlib
import io
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
    SearchBatch,
    SearchHit,
)
from opensac_sdk.search import SearchResource
from opensac_sdk.state import StateResource

from opensac.sandbox.validator import validate_code

SKILL_PATH = Path(__file__).parents[1] / "skills" / "search-as-code" / "SKILL.md"


def _pattern() -> str:
    skill = SKILL_PATH.read_text(encoding="utf-8")
    return skill.split("## Pattern", 1)[1].split("```python", 1)[1].split("```", 1)[0]


class FakeSearch:
    def __init__(self) -> None:
        self._resource = SearchResource(None)  # type: ignore[arg-type]

    def many(self, queries: list[str], **_kwargs: object) -> list[SearchBatch]:
        return [
            SearchBatch(
                query=query,
                hits=[
                    SearchHit(
                        ref=f"ref_unique_{index}",
                        backend="local",
                        title=f"unique {index}",
                        date="1998",
                        rank=1,
                    ),
                    SearchHit(
                        ref="ref_consensus",
                        backend="local",
                        title="consensus",
                        date="1999",
                        rank=9,
                    ),
                ],
            )
            for index, query in enumerate(queries)
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
    ) -> None:
        self.missing_year = missing_year
        self.same_ref = same_ref
        self.year_from_turn = year_from_turn
        self.partial_failure = partial_failure
        self.locator_exhausted = locator_exhausted
        self.turn = 1
        self.grep_widths: list[int] = []

    def grep_report(self, refs: list[str], pattern: str, **_kwargs: object) -> ContentGrepReport:
        self.grep_widths.append(len(refs))
        is_year = "1998" in pattern
        if is_year and (self.missing_year or self.turn < self.year_from_turn):
            matches = []
        else:
            ref = refs[0] if self.same_ref or not is_year else refs[1]
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

    def grep(self, refs: list[str], pattern: str, **kwargs: object) -> list[ContentMatch]:
        return self.grep_report(refs, pattern, **kwargs).matches

    def read(self, refs: list[str], **kwargs: object) -> list[ContentSnippet]:
        ref = refs[0]
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
    missing_year: bool = False,
    same_ref: bool = False,
    year_from_turn: int = 1,
    turns: int = 1,
    partial_failure: bool = False,
    locator_exhausted: bool = False,
):
    content = FakeContent(
        missing_year=missing_year,
        same_ref=same_ref,
        year_from_turn=year_from_turn,
        partial_failure=partial_failure,
        locator_exhausted=locator_exhausted,
    )
    output = FakeOutput()
    sdk = SimpleNamespace(
        search=FakeSearch(),
        content=content,
        output=output,
        state=StateResource(str(tmp_path)),
    )
    module = ModuleType("opensac_sdk")
    module.BrokerError = BrokerError
    module.sdk = sdk
    printed = io.StringIO()
    with patch.dict(sys.modules, {"opensac_sdk": module}), contextlib.redirect_stdout(printed):
        for turn in range(1, turns + 1):
            content.turn = turn
            exec(compile(_pattern(), "<search-as-code-skill>", "exec"), {})
    return sdk, content, output, printed.getvalue()


def test_pattern_is_bounded_and_teaches_the_04_contract() -> None:
    skill = SKILL_PATH.read_text(encoding="utf-8")
    pattern = _pattern()

    assert len(skill) < 8_000
    compile(pattern, "<search-as-code-skill>", "exec")
    validate_code(pattern)
    assert "sdk.search.fuse_rrf(batches, k=60)" in pattern
    assert "fuse_rrf(batches, k=60, limit=" not in pattern
    assert 'sdk.state.merge_jsonl("pool.jsonl"' in pattern
    assert "sdk.content.grep_report(list(pool)" in pattern
    assert "failed.failure.code" in pattern
    assert "passage.locator_error" in pattern
    assert 'row["score"] = max(row["score"], candidate.fused_score)' in pattern
    assert 'if not pool:' in pattern
    assert 'sdk.state.write_json("evidence.json", evidence)' in pattern
    assert '"locator": passage.locator.model_dump(mode="json")' in pattern
    assert "evidence_key" not in pattern
    assert "evidence.jsonl" not in pattern


def test_pattern_keeps_one_ranked_pool_and_cites_read_passages(tmp_path: Path) -> None:
    sdk, content, output, printed = _run_pattern(tmp_path, turns=2)

    pool = sdk.state.read_jsonl("pool.jsonl")
    consensus = next(row for row in pool if row.ref == "ref_consensus")
    assert len(pool) == 3
    assert set(consensus) == {"ref", "title", "date", "score"}
    assert consensus.score > 0
    assert content.grep_widths == [3, 3]
    assert printed.splitlines()[1].endswith("consensus")
    assert len(output.submissions) == 2
    assert all(
        citation["ref"] == citation["locator"]["ref"]
        and citation["locator"]["id"].startswith("read-")
        for _, citations in output.submissions
        for citation in citations
    )


def test_pattern_pool_score_is_idempotent_across_replayed_turns(tmp_path: Path) -> None:
    sdk, _, _, _ = _run_pattern(tmp_path, turns=1)
    first = {row.ref: row.score for row in sdk.state.read_jsonl("pool.jsonl")}

    sdk, _, _, _ = _run_pattern(tmp_path, turns=2)
    replayed = {row.ref: row.score for row in sdk.state.read_jsonl("pool.jsonl")}

    assert replayed == first


def test_pattern_does_not_submit_with_an_unsupported_constraint(tmp_path: Path) -> None:
    _, _, output, printed = _run_pattern(tmp_path, missing_year=True)

    assert "unverified: ['year']" in printed
    assert not output.submissions


def test_pattern_verifies_far_apart_constraints_in_the_same_document(tmp_path: Path) -> None:
    sdk, _, output, _ = _run_pattern(tmp_path, same_ref=True)

    ledger = sdk.state.read_json("evidence.json")
    assert set(ledger) == {"phrase", "year"}
    assert all(
        set(row) == {"pattern", "ref", "text", "locator"}
        for row in ledger.values()
    )
    assert len(output.submissions) == 1
    _, citations = output.submissions[0]
    assert len(citations) == 2
    assert len({citation["locator"]["id"] for citation in citations}) == 2


def test_pattern_unions_evidence_across_turns_before_submitting(tmp_path: Path) -> None:
    sdk, _, output, printed = _run_pattern(tmp_path, year_from_turn=2, turns=2)

    assert "unverified: ['year']" in printed
    assert len(output.submissions) == 1
    submitted, citations = output.submissions[0]
    assert {row["constraint"] for row in submitted["evidence"]} == {"phrase", "year"}
    assert len(citations) == 2
    assert set(sdk.state.read_json("evidence.json")) == {"phrase", "year"}


def test_pattern_reports_typed_partial_fetch_failure_and_keeps_matches(
    tmp_path: Path,
) -> None:
    _, _, output, printed = _run_pattern(tmp_path, partial_failure=True)

    assert "code=provider_timeout" in printed
    assert len(output.submissions) == 1


def test_pattern_never_cites_locator_capacity_exhausted_text(tmp_path: Path) -> None:
    sdk, _, output, printed = _run_pattern(tmp_path, locator_exhausted=True)

    assert "locator unavailable: evidence_capacity_exhausted" in printed
    assert "unverified: ['phrase', 'year']" in printed
    assert not output.submissions
    assert not sdk.state.exists("evidence.json")


def test_pattern_invalidates_evidence_when_constraint_pattern_changes(
    tmp_path: Path,
) -> None:
    state = StateResource(str(tmp_path))
    state.write_json(
        "evidence.json",
        {
            "year": {
                "pattern": "stale pattern",
                "ref": "ref_stale",
                "text": "stale evidence",
                "locator": {
                    "id": "stale",
                    "ref": "ref_stale",
                    "kind": "selected_passage",
                },
            }
        },
    )

    sdk, _, output, _ = _run_pattern(tmp_path, missing_year=True)

    assert not output.submissions
    assert set(sdk.state.read_json("evidence.json")) == {"phrase"}
