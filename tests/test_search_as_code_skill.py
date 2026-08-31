from __future__ import annotations

import contextlib
import io
import re
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from opensac_sdk._record import Record, record
from opensac_sdk._resources import SearchResource, WorkspaceResource

from opensac.backends.search import SearchHit
from opensac.sandbox.validator import validate_code

ROOT = Path(__file__).parents[1]
SKILL_DIR = ROOT / ".agents" / "skills" / "search-as-code"
SKILL_PATH = SKILL_DIR / "SKILL.md"
CONTRACT_PATH = SKILL_DIR / "references" / "sdk-contract.md"
ORCHESTRATION_PATH = SKILL_DIR / "references" / "orchestration.md"
REPEATED_UNITS_PATH = SKILL_DIR / "references" / "repeated-units.md"
STATEFUL_PROGRAM_PATH = ROOT / "tests" / "data" / "search_as_code_stateful_program.py"


def _code_block(path: Path, heading: str) -> str:
    text = path.read_text(encoding="utf-8")
    section = text.split(heading, 1)[1]
    return section.split("```python", 1)[1].split("```", 1)[0]


def _repeated_units_pattern() -> str:
    return _code_block(REPEATED_UNITS_PATH, "## Gate fan-out and preserve record sets")


def _stateful_pattern() -> str:
    return STATEFUL_PROGRAM_PATH.read_text(encoding="utf-8")


def _artifact(workspace: WorkspaceResource, name: str) -> str:
    matches = [path for path in workspace.list() if path.endswith(f"/{name}")]
    assert len(matches) == 1
    return matches[0]


class FakeSearch:
    def __init__(self, *, vary_by_turn: bool = False, hits_per_query: int = 2) -> None:
        self._resource = SearchResource(None)  # type: ignore[arg-type]
        self.vary_by_turn = vary_by_turn
        self.hits_per_query = hits_per_query
        self.turn = 1
        self.many_calls: list[tuple[str, ...]] = []

    @staticmethod
    def _result(hits: list[SearchHit]) -> list[Record]:
        return [record(hit.model_dump(mode="json")) for hit in hits]

    def many(self, queries: list[str], **_kwargs: object) -> list[list[Record] | None]:
        self.many_calls.append(tuple(queries))
        prefix = f"turn_{self.turn}_" if self.vary_by_turn else ""
        if self.hits_per_query == 2:
            return [
                self._result(
                    [
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
                for index, _query in enumerate(queries)
            ]

        return [
            self._result(
                [
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
            for query_index, _query in enumerate(queries)
        ]

    def fuse_rrf(
        self,
        queries: list[str],
        results: list[list[Record] | None],
        **kwargs: object,
    ):
        return self._resource.fuse_rrf(queries, results, **kwargs)


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
        long_documents: bool = False,
    ) -> None:
        self.missing_year = missing_year
        self.same_source = same_source
        self.year_from_turn = year_from_turn
        self.partial_failure = partial_failure
        self.no_matches = no_matches
        self.grep_error = grep_error
        self.long_documents = long_documents
        self.turn = 1
        self.grep_calls: list[tuple[str, tuple[str, ...]]] = []
        self.passage_calls: list[tuple[str, tuple[str, ...]]] = []
        self.fetch_sources: list[str] = []
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
    ) -> list[Record | None]:
        self.grep_calls.append((pattern, tuple(sources)))
        if self.grep_error:
            return [None] * len(sources)
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
        results = []
        for index, source in enumerate(sources):
            if index == failed_index:
                results.append(None)
                continue
            results.append(
                record(
                    {
                        "source": source,
                        "title": source,
                        "matches": [match] if index == matched_index and match is not None else [],
                        "next_start_line": None,
                    }
                )
            )
        return results

    def fetch(self, source: str) -> Record | None:
        self.fetch_sources.append(source)
        if self.partial_failure and source.endswith("2"):
            return None
        parts = []
        if not self.no_matches:
            if self.same_source or source.endswith("1"):
                parts.append(f"target phrase evidence for {source}")
            if (
                not self.missing_year
                and self.turn >= self.year_from_turn
                and (self.same_source or source.endswith("2"))
            ):
                parts.append(f"1998 evidence for {source}")
        text = "\n".join(parts) or f"unrelated evidence for {source}"
        if self.long_documents:
            text = f"{'x' * 5_000}\n{text}\n{'y' * 5_000}"
        return record(
            {
                "source": source,
                "title": source,
                "date": "1998",
                "text": text,
                "metadata": {},
            }
        )

    def fetch_many(self, sources: list[str], *, concurrency: int = 5) -> list[Record | None]:
        assert concurrency >= 1
        return [self.fetch(source) for source in sources]

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
    long_documents: bool = False,
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
        long_documents=long_documents,
    )
    sdk = SimpleNamespace(
        search=search,
        content=content,
        workspace=WorkspaceResource(str(tmp_path)),
    )
    module = ModuleType("opensac_sdk")
    module.sdk = sdk
    printed = io.StringIO()
    source = program or _stateful_pattern()
    with patch.dict(sys.modules, {"opensac_sdk": module}), contextlib.redirect_stdout(printed):
        for turn in range(1, turns + 1):
            search.turn = turn
            content.turn = turn
            exec(compile(source, "<search-as-code-pattern>", "exec"), {})
    return sdk, content, printed.getvalue()


def test_skill_keeps_the_core_contract_small_and_schema_neutral() -> None:
    skill = SKILL_PATH.read_text(encoding="utf-8")
    description = skill.splitlines()[2]
    linked_references = {
        target.split("#", 1)[0] for target in re.findall(r"\]\((references/[^)]+)\)", skill)
    }

    assert len(skill) < 7_000
    assert "fact checking" in description
    assert "Codex" not in description
    assert "Claude" not in description
    assert {
        "references/sdk-contract.md",
        "references/orchestration.md",
        "references/repeated-units.md",
    } <= linked_references
    assert all((SKILL_DIR / target).is_file() for target in linked_references)
    assert all(
        symbol in skill
        for symbol in (
            "sac_run(code)",
            "sdk.search",
            "sdk.content.fetch",
            "sdk.workspace",
            "result or `None`",
            "zip(inputs, results, strict=True)",
            "state_lost",
        )
    )
    assert not any(token in skill for token in ("NEXT:", "READY:", "ERROR:"))
    assert all(
        fixed_name not in skill
        for fixed_name in (
            "record-result.json",
            "record-units.json",
            "allowed_exclusion_codes",
            "answer_rows",
        )
    )
    assert "Use 2-4 queries" not in skill
    assert "6-12" not in skill
    assert "BrokerError" not in skill
    assert "session_id" not in skill
    assert "SAC_MCP_" not in skill


def test_orchestration_contracts_compile_and_close_local_state() -> None:
    reference = ORCHESTRATION_PATH.read_text(encoding="utf-8")
    programs = re.findall(r"```python\n(.*?)```", reference, re.DOTALL)
    assert len(programs) == 3
    namespaces = []
    for program in programs:
        namespace: dict[str, object] = {}
        exec(compile(program, "<orchestration-contract>", "exec"), namespace)
        namespaces.append(namespace)

    run_candidates = namespaces[0]["run_parser_candidates"]
    result = run_candidates(  # type: ignore[operator]
        "body",
        [("bad", lambda _: []), ("good", lambda _: [{"key": "row"}])],
        lambda rows: [] if len(rows) == 1 else ["cardinality"],
    )
    assert result == {
        "state": "supported",
        "rows": [{"key": "row"}],
        "attempts": [
            {"name": "bad", "rows": 0, "problems": ["cardinality"]},
            {"name": "good", "rows": 1, "problems": []},
        ],
    }

    bind_selected = namespaces[1]["bind_selected_artifact"]
    body, problems = bind_selected(  # type: ignore[operator]
        {"source": "source-a", "artifact": "selected.json"},
        "other.json",
        {"source": "source-b", "body": "body"},
    )
    assert body == ""
    assert problems == ["selected_artifact_path", "selected_artifact_source"]

    finalize = namespaces[2]["finalize_scoped_claim"]
    base = {
        "subject": "entity",
        "predicate": "built_for",
        "scope": {"role": "original", "time": "1904"},
    }
    context_only = finalize(  # type: ignore[operator]
        {
            **base,
            "evidence": [
                {
                    "subject": "entity",
                    "predicate": "occupied_by",
                    "scope": {"role": "later", "time": "1931"},
                    "stance": "context",
                }
            ],
        }
    )
    assert context_only["state"] == "unknown"
    unvalidated_support = finalize(  # type: ignore[operator]
        {
            **base,
            "evidence": [
                {
                    "subject": "entity",
                    "predicate": "built_for",
                    "scope": base["scope"],
                    "stance": "supports",
                    "validated": False,
                }
            ],
        }
    )
    assert unvalidated_support["state"] == "unknown"
    contradicted = finalize(  # type: ignore[operator]
        {
            **base,
            "evidence": [
                {
                    "subject": "entity",
                    "predicate": "built_for",
                    "scope": base["scope"],
                    "stance": "contradicts",
                    "validated": True,
                }
            ],
        }
    )
    assert contradicted["state"] == "contradicted"
    assert contradicted["conflict"] is False


def test_skill_has_codex_catalog_metadata() -> None:
    metadata = (SKILL_DIR / "agents" / "openai.yaml").read_text(encoding="utf-8")

    assert 'display_name: "Search as Code"' in metadata
    assert 'short_description: "Research with OpenSAC MCP and source URLs"' in metadata
    assert "$search-as-code" in metadata


def test_contract_documents_mapping_records_and_workspace() -> None:
    contract = CONTRACT_PATH.read_text(encoding="utf-8")

    assert "opensac_sdk.types" not in contract
    assert "Mapping access is canonical" in contract
    assert "non-colliding fields" in contract
    assert "Fused candidate" in contract
    assert "sdk.content.passages(" not in contract
    assert "structured session-workspace interface" in contract
    assert "sdk.workspace" in contract
    assert "sdk.state" not in contract
    assert "sdk.output" not in contract
    assert "Adapter failures occur outside the sandbox" in contract
    assert "0-based, end-exclusive" in contract
    assert "`read.window.next`" in contract
    assert "Search result list" in contract
    assert "Grep result list" in contract
    assert "sdk.content.fetch(source)" in contract
    assert "sdk.content.fetch_many(sources, *, concurrency=5)" in contract
    assert "Fetch result list" in contract
    assert "return `None`" in contract
    assert "Check `is None`, never truthiness" in contract
    assert "Do not add `try/except`" in contract
    assert "sdk.session" not in contract


def test_contract_omits_sdk_capabilities_not_taught_by_the_skill() -> None:
    contract = CONTRACT_PATH.read_text(encoding="utf-8")

    assert "sdk.content.passages(" not in contract
    assert "llm.complete" not in contract
    assert "sdk.llm." not in contract
    assert "LLM call" not in contract


def test_reference_programs_compile_and_pass_sandbox_validation() -> None:
    programs = {
        "repeated-units": _repeated_units_pattern(),
        "stateful-fixture": _stateful_pattern(),
    }

    for name, program in programs.items():
        compile(program, f"<search-as-code-{name}-pattern>", "exec")
        validate_code(program)
        assert not any(token in program for token in ("NEXT:", "READY:", "ERROR:"))

    assert "sdk.workspace." in programs["stateful-fixture"]


def test_repeated_unit_helpers_gate_fanout_and_preserve_multiple_records() -> None:
    namespace: dict[str, object] = {}
    exec(compile(_repeated_units_pattern(), "<repeated-units>", "exec"), namespace)

    source_rows = [
        {"key": "alpha", "membership": "supported"},
        {"key": "beta", "membership": "supported"},
    ]
    gated, problems = namespace["gate_units"](  # type: ignore[operator]
        source_rows, expected_count=2
    )
    assert gated == source_rows
    assert problems == []
    gated, problems = namespace["gate_units"](  # type: ignore[operator]
        source_rows[:1], expected_count=2
    )
    assert gated == []
    assert problems == ["cardinality"]

    units = [
        {
            "key": "alpha",
            "requested_fields": ["degree", "field"],
            "records": [
                {
                    "key": "degree-one",
                    "fields": {
                        "degree": {"state": "supported", "value": "BS"},
                        "field": {"state": "supported", "value": "Field One"},
                    },
                    "evidence": [{"source": "source-a", "excerpt": "earned BS in Field One"}],
                },
                {
                    "key": "degree-two",
                    "fields": {
                        "degree": {"state": "supported", "value": "BS"},
                        "field": {"state": "missing", "value": ""},
                    },
                    "evidence": [{"source": "source-a", "excerpt": "earned a second BS"}],
                },
            ],
            "unresolved_mentions": [],
            "exclusions": [
                {
                    "validated": True,
                    "source": "source-a",
                    "excerpt": "honorary doctorate",
                    "reason": "outside requested earned degrees",
                }
            ],
            "scope_complete": True,
        },
        {
            "key": "beta",
            "requested_fields": ["degree", "field"],
            "records": [],
            "unresolved_mentions": ["possible degree"],
            "exclusions": [],
            "scope_complete": True,
        },
    ]
    result = namespace["finalize_record_units"](units)  # type: ignore[operator]

    assert [row["record_key"] for row in result["answer_rows"]] == [
        "degree-one",
        "degree-two",
    ]
    assert result["coverage"] == {
        "units": 2,
        "records": 2,
        "complete_units": 1,
        "field_states": {"supported": 3, "missing": 1},
    }
    assert result["unit_rows"][0]["complete"] is True
    assert result["unit_rows"][1]["problems"] == ["unresolved_mentions"]


def test_pattern_keeps_one_ranked_pool_and_prints_sources_for_read_passages(
    tmp_path: Path,
) -> None:
    sdk, content, printed = _run_pattern(tmp_path)

    pool = sdk.workspace.read_jsonl(_artifact(sdk.workspace, "pool.jsonl"))
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

    evidence = sdk.workspace.read_json(_artifact(sdk.workspace, "evidence.json"))
    assert set(evidence) == {"phrase", "year"}
    assert all(row.source in printed for row in evidence.values())
    assert "unsupported: none" in printed


def test_pattern_pool_score_is_idempotent_across_replayed_stages(tmp_path: Path) -> None:
    sdk, _, _ = _run_pattern(tmp_path)
    pool_path = _artifact(sdk.workspace, "pool.jsonl")
    first = {row.source: row.score for row in sdk.workspace.read_jsonl(pool_path)}

    sdk, _, _ = _run_pattern(tmp_path, turns=2)
    replayed = {
        row.source: row.score
        for row in sdk.workspace.read_jsonl(_artifact(sdk.workspace, "pool.jsonl"))
    }

    assert replayed == first


def test_pattern_reports_an_unsupported_constraint_without_completion_token(tmp_path: Path) -> None:
    sdk, _, printed = _run_pattern(tmp_path, missing_year=True)

    assert "unsupported: ['year']" in printed
    assert not any(token in _stateful_pattern() for token in ("NEXT:", "READY:", "ERROR:"))
    evidence = sdk.workspace.read_json(_artifact(sdk.workspace, "evidence.json"))
    assert set(evidence) == {"phrase"}
    attempts = sdk.workspace.read_json(_artifact(sdk.workspace, "attempts.json"))
    assert attempts.year.fingerprint
    assert attempts.year.sources


def test_pattern_verifies_far_apart_constraints_in_the_same_document(tmp_path: Path) -> None:
    sdk, _, printed = _run_pattern(tmp_path, same_source=True)

    evidence = sdk.workspace.read_json(_artifact(sdk.workspace, "evidence.json"))
    assert {row.source for row in evidence.values()} == {"doc_consensus"}
    assert all(
        set(row) == {"fingerprint", "requirement", "source", "text"} for row in evidence.values()
    )
    assert "source='doc_consensus'" in printed
    assert "unsupported: none" in printed


def test_pattern_unions_new_evidence_across_turns(tmp_path: Path) -> None:
    sdk, _, printed = _run_pattern(
        tmp_path,
        year_from_turn=2,
        turns=2,
        vary_search_by_turn=True,
    )

    assert "unsupported: ['year']" in printed
    assert "EVIDENCE phrase:" in printed
    assert "EVIDENCE year:" in printed
    assert "unsupported: none" in printed
    evidence = sdk.workspace.read_json(_artifact(sdk.workspace, "evidence.json"))
    assert set(evidence) == {"phrase", "year"}
    assert evidence.phrase.source.startswith("doc_turn_1_")
    assert evidence.year.source.startswith("doc_turn_2_")


def test_pattern_keeps_matches_after_partial_fetch_failure(tmp_path: Path) -> None:
    _, _, printed = _run_pattern(tmp_path, partial_failure=True)

    assert "provider_timeout" not in printed
    assert "unsupported: none" in printed


def test_pattern_bounds_pool_and_content_batches(tmp_path: Path) -> None:
    sdk, content, _ = _run_pattern(
        tmp_path,
        turns=9,
        no_matches=True,
        vary_search_by_turn=True,
        hits_per_query=10,
    )

    pool = sdk.workspace.read_jsonl(_artifact(sdk.workspace, "pool.jsonl"))
    assert len(pool) == 200
    assert content.grep_widths
    assert max(content.grep_widths) <= 40


def test_pattern_does_not_rescan_attempted_sources(tmp_path: Path) -> None:
    _, content, printed = _run_pattern(
        tmp_path,
        turns=2,
        missing_year=True,
    )

    year_calls = [sources for pattern, sources in content.grep_calls if "1998" in pattern]
    assert len(year_calls) == 1
    assert "year: no untried candidates; change the queries" in printed


def test_pattern_does_not_replay_sources_after_call_wide_content_failure(tmp_path: Path) -> None:
    _, content, printed = _run_pattern(
        tmp_path,
        turns=2,
        grep_error=True,
    )

    assert len(content.grep_calls) == 2
    assert "provider_unavailable" not in printed
    assert printed.count("no untried candidates; change the queries") == 2


def test_pattern_isolates_a_changed_task_in_a_new_workspace_namespace(tmp_path: Path) -> None:
    original = _stateful_pattern()
    changed = original.replace(
        "Identify the target entity and verify the requested phrase and year.",
        "Identify a different entity while verifying the same phrase and year.",
    )
    assert changed != original

    _run_pattern(tmp_path, program=original)
    sdk, _, _ = _run_pattern(tmp_path, program=changed)

    pools = [path for path in sdk.workspace.list() if path.endswith("/pool.jsonl")]
    manifests = [path for path in sdk.workspace.list() if path.endswith("/manifest.json")]
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
    sdk, content, printed = _run_pattern(tmp_path, program=changed)

    assert len([path for path in sdk.workspace.list() if path.endswith("/pool.jsonl")]) == 1
    assert len([path for path in sdk.workspace.list() if path.endswith("/manifest.json")]) == 1
    assert any(pattern == "target phrase" for pattern, _ in content.grep_calls)
    evidence = sdk.workspace.read_json(_artifact(sdk.workspace, "evidence.json"))
    assert set(evidence) == {"phrase", "year"}
    assert "unsupported: none" in printed
