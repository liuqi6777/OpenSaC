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

from opensac.backends.search import SearchHit
from opensac.sandbox.validator import validate_code

ROOT = Path(__file__).parents[1]
SKILL_DIR = ROOT / ".agents" / "skills" / "search-as-code"
SKILL_PATH = SKILL_DIR / "SKILL.md"
CONTRACT_PATH = SKILL_DIR / "references" / "sdk-contract.md"
PATTERNS_PATH = SKILL_DIR / "references" / "patterns.md"
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


def _cache_pattern() -> str:
    return _code_block(PATTERNS_PATH, "## Optionally cache selected fetches across calls")


def _extract_pattern() -> str:
    return _code_block(
        PATTERNS_PATH,
        "## Optionally extract structured fields from inspected evidence",
    )


def _stateful_pattern() -> str:
    return STATEFUL_PROGRAM_PATH.read_text(encoding="utf-8")


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

    @staticmethod
    def _success(query: str, hits: list[SearchHit]) -> Record:
        return record(
            {
                "query": query,
                "status": "success",
                "hits": [hit.model_dump(mode="json") for hit in hits],
                "error": None,
            }
        )

    def many(self, queries: list[str], **_kwargs: object) -> list[Record]:
        self.many_calls.append(tuple(queries))
        prefix = f"turn_{self.turn}_" if self.vary_by_turn else ""
        if self.hits_per_query == 2:
            return [
                self._success(
                    query,
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
                for index, query in enumerate(queries)
            ]

        return [
            self._success(
                query,
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
            for query_index, query in enumerate(queries)
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

    def fetch(self, source: str) -> Record:
        self.fetch_sources.append(source)
        if self.partial_failure and source.endswith("2"):
            raise BrokerError(
                "content provider unavailable",
                code="provider_timeout",
                retryable=True,
                attempts=3,
            )
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

    def fetch_many(self, sources: list[str], *, concurrency: int = 5) -> list[Record]:
        assert concurrency >= 1
        outcomes = []
        for source in sources:
            try:
                document = self.fetch(source)
            except BrokerError as error:
                outcomes.append(
                    record(
                        {
                            "source": source,
                            "status": "failure",
                            "document": None,
                            "error": {"code": error.code},
                        }
                    )
                )
            else:
                outcomes.append(
                    record(
                        {
                            "source": source,
                            "status": "success",
                            "document": document,
                            "error": None,
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
        state=StateResource(str(tmp_path)),
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
    return sdk, content, printed.getvalue()


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
    assert "Read deployment capabilities with `sdk.capabilities()`" in flat_skill
    assert "sdk.session" not in flat_skill
    assert "sdk.output" not in flat_skill
    assert "sdk.workspace" not in flat_skill
    assert "state_lost" in flat_skill
    assert "program was not replayed" in flat_skill
    assert "execution outcome may be" in flat_skill
    assert "same program blindly" in flat_skill
    assert "Public web URLs" in flat_skill
    assert "references/sdk-contract.md" in flat_skill
    assert "references/patterns.md" in flat_skill
    assert "references/advanced.md" not in flat_skill
    assert "references/python-recipes.md" not in flat_skill
    assert "references/stateful-research.md" not in flat_skill
    assert "optionally-cache-selected-fetches-across-calls" in flat_skill
    assert "Choose the strategy yourself" in flat_skill
    assert "teaches how to encode it as OpenSAC code" in flat_skill
    assert "No fixed query count, capability" in flat_skill
    assert "stage split, or workspace schema is required" in flat_skill
    assert "Use ordinary Python freely for deterministic orchestration" in flat_skill
    assert "this is not a required sequence or policy" in flat_skill
    assert "Issue another search batch only" not in flat_skill
    assert "Once useful authoritative candidates exist" not in flat_skill
    assert "Agent completion is the final response to the user" in flat_skill
    assert "Once printed evidence covers the request" in flat_skill
    assert "Material claims, evidence, status, and source strings in stdout" in flat_skill
    assert "Prefer a small data cache over a workflow state machine" in flat_skill
    assert "filter repeated queries or sources" in flat_skill
    assert "Keep each cache cumulative" in flat_skill
    assert "Do not print raw result lists, full documents, or the ledger" in flat_skill
    assert "Runtime metrics alone" in flat_skill
    assert "not as new evidence" in flat_skill
    assert "program-to-program memory" in flat_skill
    assert "observations show artifact paths, not their" in flat_skill
    assert "sdk.content.passages" not in flat_skill
    assert "sdk.content.grep" in flat_skill
    assert "sdk.content.read" in flat_skill
    assert "candidates, not a fetch queue" in flat_skill
    assert "smallest source-diverse set" in flat_skill
    assert "Do not fetch the whole result list" in flat_skill
    assert "`sdk.content.fetch_many(...)` its first content call" in flat_skill
    assert "merely to relocate text already present" in flat_skill
    assert "across the whole program" in flat_skill
    assert "Never pass an unfetched source" in flat_skill
    assert "Treat every example as a starting point rather than a required pipeline" in flat_skill
    assert "fact checking" in skill.split("---", 2)[1]
    assert "Use 2-4 queries" not in flat_skill
    assert "6-12" not in flat_skill
    assert "Before ending with `NEXT:`" not in flat_skill
    assert "session_id" not in flat_skill
    assert "SAC_MCP_" not in flat_skill


def test_example_references_are_explicitly_non_prescriptive() -> None:
    patterns = PATTERNS_PATH.read_text(encoding="utf-8")
    flat_patterns = " ".join(patterns.split())

    assert "not a required pipeline" in flat_patterns
    assert "query count, bounds, call grouping, and stopping point are examples" in flat_patterns
    assert "selected sources are inputs" in flat_patterns
    assert "not a search or stopping policy" in flat_patterns


def test_skill_has_codex_catalog_metadata() -> None:
    metadata = (SKILL_DIR / "agents" / "openai.yaml").read_text(encoding="utf-8")

    assert 'display_name: "Search as Code"' in metadata
    assert 'short_description: "Research with OpenSAC MCP and source URLs"' in metadata
    assert "$search-as-code" in metadata


def test_contract_documents_mapping_records_and_state() -> None:
    contract = CONTRACT_PATH.read_text(encoding="utf-8")

    assert "opensac_sdk.types" not in contract
    assert "Mapping access is canonical" in contract
    assert "non-colliding fields" in contract
    assert "Fused candidate" in contract
    assert "sdk.content.passages(" not in contract
    assert "structured session-workspace interface" in contract
    assert "sdk.workspace" not in contract
    assert "sdk.output" not in contract
    assert "Adapter failures occur outside the sandbox" in contract
    assert "0-based, end-exclusive" in contract
    assert "`read.window.next`" in contract
    assert "Search outcome list" in contract
    assert "Grep outcome list" in contract
    assert "sdk.content.fetch(source)" in contract
    assert "sdk.content.fetch_many(sources, *, concurrency=5)" in contract
    assert "Fetch outcome list" in contract
    assert "never print a complete fetched document" in contract
    assert "failed rows use `outcome.error`" in contract
    assert "never display or parse search `status` as failure detail" in contract
    assert "sdk.session" not in contract


def test_contract_omits_sdk_capabilities_not_taught_by_the_skill() -> None:
    contract = CONTRACT_PATH.read_text(encoding="utf-8")

    assert "sdk.content.passages(" not in contract
    assert "llm.complete" not in contract


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
    extract = _extract_pattern()
    cache = _cache_pattern()
    stateful = _stateful_pattern()

    for name, program in (
        ("explore", explore),
        ("rank", rank),
        ("verify", verify),
        ("extract", extract),
        ("cache", cache),
        ("stateful-fixture", stateful),
    ):
        compile(program, f"<search-as-code-{name}-pattern>", "exec")
        validate_code(program)

    assert len(explore.splitlines()) <= 45
    assert "sdk.search.many(" in explore
    assert "sdk.search.fuse_rrf(" in explore
    assert "fuse_rrf(outcomes, k=60)[:8]" in explore
    assert "NEXT:" in explore
    assert "sdk.content.grep(" not in explore
    assert "sdk.output" not in explore

    assert "sdk.search.many(" in rank
    assert "sdk.search.fuse_rrf(" in rank
    assert "sdk.content.fetch_many(" in rank
    assert "sdk.content.passages(" not in rank
    assert "sdk.content.read(" not in rank
    assert "for outcome in fetch_outcomes:" in rank
    assert "for document in documents.values():" in rank
    assert "sdk.output" not in rank
    assert "NEXT:" in rank
    assert "local_evidence" in rank
    assert "[:500]" in rank

    assert len(verify.splitlines()) <= 90
    assert "sdk.search.many(" not in verify
    assert "NEXT:" in verify
    assert '"source": document.source' in verify
    assert verify.index("sdk.content.fetch_many(") < verify.index("print(")
    assert "sdk.content.grep(" not in verify
    assert "sdk.content.read(" not in verify
    assert "for outcome in fetch_outcomes:" in verify
    assert "sdk.output" not in verify
    assert "structured_output_requested" not in verify
    assert "READY: synthesize the user-facing answer" in verify

    assert "sdk.state." not in explore
    assert "sdk.state." not in verify

    assert len(cache.splitlines()) <= 80
    assert "sdk.search." not in cache
    assert "sdk.content.passages(" not in cache
    assert "concurrency=" not in cache
    assert '"requested_source": requested_source' in cache
    assert "document.source" in cache
    assert 'cache_row(source, "started")' in cache
    assert cache.index("sdk.state.upsert_jsonl(") < cache.index("sdk.content.fetch_many(")
    assert cache.index("sdk.content.fetch_many(") < cache.rindex("sdk.state.upsert_jsonl(")

    assert len(extract.splitlines()) <= 55
    assert "sdk.llm.extract_many(" in extract
    assert "zip(evidence_items, outcomes, strict=True)" in extract
    assert 'quote not in item["text"]' in extract
    assert "sdk.search." not in extract
    assert "sdk.output" not in extract
    assert "followup" not in extract

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
    assert "sdk.output" not in stateful
    assert "READY: synthesize" in stateful


def test_stateful_cache_example_reuses_cumulative_artifacts(tmp_path: Path) -> None:
    program = _cache_pattern()

    sdk, content, first = _run_pattern(tmp_path, program=program)

    assert not sdk.search.many_calls
    assert content.fetch_sources == ["selected-source-url-1", "selected-source-url-2"]
    assert not content.passage_calls
    assert not content.read_sources
    assert first.count("CACHE status=success") == 2
    artifact_names = {Path(path).name for path in sdk.state.list()}
    assert artifact_names == {"fetch-cache.jsonl"}
    cached_content = sdk.state.read_jsonl("fetch-cache.jsonl")
    assert {row.requested_source for row in cached_content} == set(content.fetch_sources)
    assert all(row.status == "success" for row in cached_content)
    assert all(row.source == row.requested_source for row in cached_content)
    assert all(row.text for row in cached_content)

    resumed_sdk, resumed_content, second = _run_pattern(
        tmp_path,
        program=program,
    )

    assert not resumed_sdk.search.many_calls
    assert not resumed_content.fetch_sources
    assert not resumed_content.passage_calls
    assert not resumed_content.read_sources
    assert second.count("CACHE status=success") == 2
    assert {Path(path).name for path in resumed_sdk.state.list()} == artifact_names


def test_stateful_cache_example_persists_item_failures_without_replay(tmp_path: Path) -> None:
    program = _cache_pattern()

    sdk, content, first = _run_pattern(
        tmp_path,
        program=program,
        partial_failure=True,
    )

    assert content.fetch_sources == ["selected-source-url-1", "selected-source-url-2"]
    cached = {row.requested_source: row for row in sdk.state.read_jsonl("fetch-cache.jsonl")}
    assert cached["selected-source-url-1"].status == "success"
    assert cached["selected-source-url-2"].status == "failure"
    assert cached["selected-source-url-2"].error.code == "provider_timeout"
    assert "error=provider_timeout" in first

    _, resumed_content, second = _run_pattern(tmp_path, program=program)

    assert not resumed_content.fetch_sources
    assert "status=failure" in second


def test_composed_pattern_fetches_only_its_selected_subset_for_local_inspection(
    tmp_path: Path,
) -> None:
    _, content, printed = _run_pattern(tmp_path, program=_rank_pattern())

    assert 1 <= len(content.fetch_sources) <= 4
    assert not content.passage_calls
    assert not content.grep_calls
    assert not content.read_sources
    assert "select another small relevant batch" in printed


def test_explore_pattern_stops_for_model_judgment(tmp_path: Path) -> None:
    _, content, printed = _run_pattern(tmp_path, program=_explore_pattern())

    lines = printed.strip().splitlines()
    assert 1 <= sum(line.startswith("CANDIDATE ") for line in lines) <= 8
    assert lines[-1].startswith("NEXT: choose a small relevant subset")
    assert not content.grep_calls


def test_verify_pattern_returns_runtime_evidence_for_model_synthesis(tmp_path: Path) -> None:
    sdk, content, printed = _run_pattern(tmp_path, program=_verify_pattern())

    assert content.fetch_sources == ["selected-source-url-1", "selected-source-url-2"]
    assert not content.grep_calls
    assert not content.read_sources
    assert "EVIDENCE phrase:" in printed
    assert "EVIDENCE year:" in printed
    assert printed.strip().endswith(
        "READY: synthesize the user-facing answer from this verified evidence"
    )
    assert not hasattr(sdk, "output")


def test_verify_pattern_ends_in_next_when_model_judgment_is_needed(tmp_path: Path) -> None:
    _, _, printed = _run_pattern(
        tmp_path,
        program=_verify_pattern(),
        missing_year=True,
    )

    lines = printed.strip().splitlines()
    assert lines[0].startswith("EVIDENCE phrase:")
    assert lines[-1].startswith("NEXT:")
    assert "missing=['year']" in lines[-1]


def test_verify_pattern_keeps_evidence_after_one_fetch_fails(tmp_path: Path) -> None:
    _, content, printed = _run_pattern(
        tmp_path,
        program=_verify_pattern(),
        partial_failure=True,
    )

    assert content.fetch_sources == ["selected-source-url-1", "selected-source-url-2"]
    assert "EVIDENCE phrase:" in printed
    assert "selected-source-url-2:fetch:provider_timeout" in printed
    assert "missing=['year']" in printed


def test_verify_pattern_bounds_whole_document_output(tmp_path: Path) -> None:
    _, content, printed = _run_pattern(
        tmp_path,
        program=_verify_pattern(),
        long_documents=True,
    )

    assert content.fetch_sources == ["selected-source-url-1", "selected-source-url-2"]
    assert "EVIDENCE phrase:" in printed
    assert "EVIDENCE year:" in printed
    assert len(printed) < 4_000
    assert "x" * 1_000 not in printed
    assert "y" * 1_000 not in printed


def test_pattern_keeps_one_ranked_pool_and_prints_sources_for_read_passages(
    tmp_path: Path,
) -> None:
    sdk, content, printed = _run_pattern(tmp_path)

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

    evidence = sdk.state.read_json(_artifact(sdk.state, "evidence.json"))
    assert set(evidence) == {"phrase", "year"}
    assert all(row.source in printed for row in evidence.values())
    assert "READY: synthesize" in printed


def test_pattern_pool_score_is_idempotent_across_replayed_stages(tmp_path: Path) -> None:
    sdk, _, _ = _run_pattern(tmp_path)
    pool_path = _artifact(sdk.state, "pool.jsonl")
    first = {row.source: row.score for row in sdk.state.read_jsonl(pool_path)}

    sdk, _, _ = _run_pattern(tmp_path, turns=2)
    replayed = {
        row.source: row.score for row in sdk.state.read_jsonl(_artifact(sdk.state, "pool.jsonl"))
    }

    assert replayed == first


def test_pattern_does_not_emit_ready_with_an_unsupported_constraint(tmp_path: Path) -> None:
    sdk, _, printed = _run_pattern(tmp_path, missing_year=True)

    assert "unsupported: ['year']" in printed
    assert "READY:" not in printed
    evidence = sdk.state.read_json(_artifact(sdk.state, "evidence.json"))
    assert set(evidence) == {"phrase"}
    attempts = sdk.state.read_json(_artifact(sdk.state, "attempts.json"))
    assert attempts.year.fingerprint
    assert attempts.year.sources


def test_pattern_verifies_far_apart_constraints_in_the_same_document(tmp_path: Path) -> None:
    sdk, _, printed = _run_pattern(tmp_path, same_source=True)

    evidence = sdk.state.read_json(_artifact(sdk.state, "evidence.json"))
    assert {row.source for row in evidence.values()} == {"doc_consensus"}
    assert all(
        set(row) == {"fingerprint", "requirement", "source", "text"} for row in evidence.values()
    )
    assert "source='doc_consensus'" in printed
    assert "READY: synthesize" in printed


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
    assert "READY: synthesize" in printed
    evidence = sdk.state.read_json(_artifact(sdk.state, "evidence.json"))
    assert set(evidence) == {"phrase", "year"}
    assert evidence.phrase.source.startswith("doc_turn_1_")
    assert evidence.year.source.startswith("doc_turn_2_")


def test_pattern_reports_partial_fetch_failure_and_keeps_matches(tmp_path: Path) -> None:
    _, _, printed = _run_pattern(tmp_path, partial_failure=True)

    assert "failure[provider_timeout]" in printed
    assert "READY: synthesize" in printed


def test_pattern_bounds_pool_and_content_batches(tmp_path: Path) -> None:
    sdk, content, _ = _run_pattern(
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
    assert printed.count("code=provider_unavailable") == 2
    assert printed.count("no untried candidates; change the queries") == 2


def test_pattern_isolates_a_changed_task_in_a_new_state_namespace(tmp_path: Path) -> None:
    original = _stateful_pattern()
    changed = original.replace(
        "Identify the target entity and verify the requested phrase and year.",
        "Identify a different entity while verifying the same phrase and year.",
    )
    assert changed != original

    _run_pattern(tmp_path, program=original)
    sdk, _, _ = _run_pattern(tmp_path, program=changed)

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
    sdk, content, printed = _run_pattern(tmp_path, program=changed)

    assert len([path for path in sdk.state.list() if path.endswith("/pool.jsonl")]) == 1
    assert len([path for path in sdk.state.list() if path.endswith("/manifest.json")]) == 1
    assert any(pattern == "target phrase" for pattern, _ in content.grep_calls)
    evidence = sdk.state.read_json(_artifact(sdk.state, "evidence.json"))
    assert set(evidence) == {"phrase", "year"}
    assert "READY: synthesize" in printed
