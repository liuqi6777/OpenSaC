from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from opensac_sdk.models import ContentSnippet, SearchHit

from opensac.broker.policy import MechanismDisabled, QuotaExceeded
from opensac.broker.service import BrokerService
from opensac.models import CAPABILITY_METHODS, Mechanisms, RunLimits, Session


class FakeBackend:
    """One hit per rank, so `offset` is observable rather than assumed.

    Ranks are absolute positions in the full result list -- the same contract
    the real backends keep -- which is what lets a test tell "the window moved"
    apart from "the window was renumbered".
    """

    def __init__(self, name: str, *, depth: int = 1) -> None:
        self.name = name
        self.depth = depth

    def _hit(self, query: str, rank: int) -> SearchHit:
        return SearchHit(
            ref="",
            backend=self.name,
            title=query,
            url=f"https://example.com/{rank}" if self.name == "web" else None,
            docid=str(rank) if self.name == "local" else None,
            snippet="snippet",
            rank=rank,
        )

    async def search(self, query, *, limit, offset=0, domains=None):
        ranks = range(offset + 1, min(offset + limit, self.depth) + 1)
        return [self._hit(query, rank) for rank in ranks]

    async def content(self, hits, *, query=None):
        return [ContentSnippet(ref=hit.ref, text=f"content:{query}", url=hit.url) for hit in hits]


class BrokenBackend:
    name = "web"

    async def search(self, query, *, limit, offset=0, domains=None):
        raise RuntimeError("backend exploded")

    async def content(self, hits, *, query=None):
        raise RuntimeError("backend exploded")


def make_session(*, backends=None, max_search_calls=2, mechanisms=None):
    return Session(
        id="sess_test",
        token="token",
        backends=backends or ["web"],
        limits=RunLimits(max_search_calls=max_search_calls),
        workspace="/tmp/session",
        mechanisms=mechanisms or Mechanisms(),
    )


async def test_broker_scopes_references_and_fetches_content() -> None:
    service = BrokerService({"web": FakeBackend("web")})
    service.register_session(make_session())
    hits = await service.call("token", "search.web", {"query": "query", "limit": 3})
    assert hits[0]["ref"].startswith("ref_")
    content = await service.call(
        "token",
        "content.snippets",
        {"refs": [hits[0]["ref"]], "query": "fact"},
    )
    assert content[0]["text"] == "content:fact"
    citations = await service.call("token", "citations.resolve", {"refs": [hits[0]["ref"]]})
    assert citations[0]["url"] == "https://example.com/1"


async def test_broker_enforces_backend_permissions() -> None:
    service = BrokerService({"local": FakeBackend("local")})
    service.register_session(make_session(backends=["web"]))
    with pytest.raises(PermissionError):
        await service.call("token", "search.local", {"query": "query"})


async def test_broker_enforces_search_quota() -> None:
    service = BrokerService({"web": FakeBackend("web")})
    service.register_session(make_session(max_search_calls=1))
    await service.call("token", "search.web", {"query": "first"})
    with pytest.raises(QuotaExceeded):
        await service.call("token", "search.web", {"query": "second"})


async def test_search_many_raises_when_every_query_fails() -> None:
    service = BrokerService({"web": BrokenBackend()})
    service.register_session(make_session(max_search_calls=5))
    with pytest.raises(RuntimeError, match="backend exploded"):
        await service.call("token", "search.web_many", {"queries": ["one", "two"]})


async def test_search_many_tolerates_partial_failure() -> None:
    service = BrokerService({"web": FakeBackend("web")})
    service.register_session(make_session(max_search_calls=5))
    # An empty query is rejected by the broker while the other one succeeds.
    batches = await service.call("token", "search.web_many", {"queries": ["ok", ""]})
    assert len(batches[0]["hits"]) == 1
    assert batches[0]["error"] is None
    assert batches[1]["hits"] == []
    assert "must not be empty" in batches[1]["error"]


class FakeModelClient:
    """Minimal stand-in for the AsyncOpenAI surface the broker touches."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        parent = self

        class Completions:
            async def create(self, **kwargs):
                parent.calls.append(kwargs)
                prompt = kwargs["messages"][-1]["content"]
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content=f"echo:{prompt}"))],
                    usage=SimpleNamespace(total_tokens=11),
                )

        self.chat = SimpleNamespace(completions=Completions())


def make_llm_service(**kwargs) -> tuple[BrokerService, FakeModelClient]:
    client = FakeModelClient()
    service = BrokerService(
        {"web": FakeBackend("web")},
        model_client=client,
        extraction_model="test-model",
    )
    service.register_session(
        Session(
            id="sess_test",
            token="token",
            backends=["web"],
            limits=RunLimits(**kwargs),
            workspace="/tmp/session",
        )
    )
    return service, client


async def test_llm_complete_passes_system_prompt_and_charges_one_call() -> None:
    service, client = make_llm_service(max_llm_calls=2)
    answer = await service.call(
        "token",
        "llm.complete",
        {"prompt": "plan the next queries", "system": "be terse", "temperature": 0.7},
    )
    assert answer == "echo:plan the next queries"
    assert client.calls[0]["messages"][0] == {"role": "system", "content": "be terse"}
    assert client.calls[0]["temperature"] == 0.7
    # complete() is free-form, so it must not force JSON mode the way extract does.
    assert "response_format" not in client.calls[0]
    assert service.sessions["token"].policy.usage.llm_calls == 1
    assert service.sessions["token"].policy.usage.pipeline_model_tokens == 11


async def test_capability_trace_records_compact_inputs_results_and_errors() -> None:
    service = BrokerService({"web": FakeBackend("web")})
    service.register_session(make_session(max_search_calls=5))
    await service.call(
        "token",
        "search.web_many",
        {"queries": ["one", "two"], "limit_per_query": 1},
        execution_id="exec-1",
    )
    with pytest.raises(ValueError):
        await service.call(
            "token",
            "content.get_many",
            {"refs": ["missing"]},
            execution_id="exec-1",
        )

    trace = service.take_trace("token", "exec-1")
    assert [event.method for event in trace] == ["search.web_many", "content.get_many"]
    assert trace[0].queries == ["one", "two"]
    assert trace[0].input_count == 2
    assert trace[0].result_count == 2
    assert trace[1].status == "error"
    assert trace[1].error_type == "ValueError"
    assert "missing" not in str(trace[0].model_dump())


async def test_query_aware_snippets_select_the_relevant_paragraph() -> None:
    class PassageBackend(FakeBackend):
        async def content(self, hits, *, query=None):
            del query
            text = (
                "An unrelated introduction about cooking and weather.\n\n"
                "Vector databases use HNSW indexes for approximate nearest-neighbor search.\n\n"
                "An unrelated conclusion about travel."
            )
            return [ContentSnippet(ref=hit.ref, text=text) for hit in hits]

    service = BrokerService({"web": PassageBackend("web")})
    state = service.register_session(make_session())
    hits = await service.call("token", "search.web", {"query": "seed"})
    snippets = await service.call(
        "token",
        "content.snippets",
        {
            "query": "HNSW nearest neighbor",
            "refs": [hits[0]["ref"]],
            "max_tokens_per_page": 12,
        },
    )
    assert "HNSW indexes" in snippets[0]["text"]
    assert "cooking" not in snippets[0]["text"]
    assert snippets[0]["metadata"]["passage_index"] == 1
    assert snippets[0]["metadata"]["passage_score"] > 0
    assert state.policy.usage.content_fetches == 1


def test_query_aware_passage_matches_shared_golden_fixture() -> None:
    fixture = json.loads(
        (Path(__file__).parent / "data" / "query_aware_passage.json").read_text(
            encoding="utf-8"
        )
    )
    text, metadata = BrokerService._select_passage(
        fixture["text"], fixture["goal"], fixture["max_chars"]
    )
    assert text == fixture["expected_text"]
    assert metadata["passage_index"] == fixture["expected_passage_index"]


async def test_llm_complete_many_preserves_prompt_order() -> None:
    service, _ = make_llm_service(max_llm_calls=5)
    answers = await service.call(
        "token",
        "llm.complete_many",
        {"prompts": ["one", "two", "three"], "concurrency": 3},
        execution_id="exec-many",
    )
    assert answers == ["echo:one", "echo:two", "echo:three"]
    assert service.sessions["token"].policy.usage.llm_calls == 3
    assert service.take_trace("token", "exec-many")[0].model_tokens == 33


async def test_llm_complete_many_charges_the_whole_fanout_before_running() -> None:
    service, client = make_llm_service(max_llm_calls=2)
    with pytest.raises(QuotaExceeded):
        await service.call("token", "llm.complete_many", {"prompts": ["one", "two", "three"]})
    # Nothing ran, so the caller is not left guessing which prompts were charged.
    assert client.calls == []
    assert service.sessions["token"].policy.usage.llm_calls == 0


async def test_llm_calls_fail_when_no_model_is_configured() -> None:
    service = BrokerService({"web": FakeBackend("web")})
    service.register_session(make_session())
    with pytest.raises(RuntimeError, match="not configured"):
        await service.call("token", "llm.complete", {"prompt": "hello"})


class RankedBackend:
    """Returns a fixed document set, so the same page recurs across queries."""

    name = "web"

    def __init__(self, urls: list[str]) -> None:
        self.urls = urls

    async def search(self, query, *, limit, offset=0, domains=None):
        return [
            SearchHit(
                ref="",
                backend="web",
                title=f"{query}-{index}",
                url=url,
                snippet="snippet",
                score=1.0 / (index + 1),
                rank=index + 1,
            )
            for index, url in enumerate(self.urls[: offset + limit])
            if index >= offset
        ]

    async def content(self, hits, *, query=None):
        return [ContentSnippet(ref=hit.ref, text="body", url=hit.url) for hit in hits]


async def test_the_same_document_keeps_one_ref_across_queries() -> None:
    """Two queries surfacing one page must hand back one handle.

    With a fresh random ref per sighting the program cannot tell that it already
    has the page, so it re-fetches it and double-counts the evidence -- and no
    two runs of the same recorded search produce the same refs, which is what
    makes a trajectory unreplayable.
    """
    service = BrokerService({"web": RankedBackend(["https://example.com/a"])})
    state = service.register_session(make_session(max_search_calls=5))

    first = await service.call("token", "search.web", {"query": "one"})
    second = await service.call("token", "search.web", {"query": "two"})

    assert first[0]["ref"] == second[0]["ref"]
    assert len(state.references) == 1


async def test_refs_are_opaque_and_reproducible() -> None:
    """Same document, different process: same handle."""
    urls = ["https://Example.com/a?utm_source=news&id=7#section"]
    left = BrokerService({"web": RankedBackend(urls)})
    left.register_session(make_session())
    right = BrokerService({"web": RankedBackend(urls)})
    right.register_session(make_session())

    one = (await left.call("token", "search.web", {"query": "q"}))[0]["ref"]
    two = (await right.call("token", "search.web", {"query": "q"}))[0]["ref"]

    assert one == two
    assert one.startswith("ref_")
    # Opaque: nothing a program could have constructed for itself.
    assert "example.com" not in one


async def test_a_docid_reaches_the_document_a_ref_reaches() -> None:
    """The handle the model can actually re-type must work.

    A ref has to be unguessable, which makes it long and random-looking, and a
    program carries it across turns by copying it through its own output. The
    docid is right there in the same hit and is what a model reaches for. Both
    now resolve to one document, so the design keeps the property it needs
    (unforgeable) without charging for one it does not (verbatim transcription).
    """
    service = BrokerService({"local": FakeBackend("local")})
    service.register_session(make_session(backends=["local"], max_search_calls=5))
    hits = await service.call("token", "search.local", {"query": "q"})

    by_ref = await service.call("token", "content.get_many", {"refs": [hits[0]["ref"]]})
    by_docid = await service.call("token", "content.get_many", {"refs": [hits[0]["docid"]]})
    by_ref_again = await service.call("token", "citations.resolve", {"refs": [hits[0]["docid"]]})

    assert by_docid == by_ref
    # A citation resolved from a docid still reports the canonical ref, so
    # provenance does not fork by which key the caller happened to use.
    assert by_ref_again[0]["ref"] == hits[0]["ref"]


async def test_a_document_this_session_never_searched_is_still_refused() -> None:
    """The admission rule is unchanged; only the lookup key is wider.

    `_resolve_refs` raising is the enforcement point of the capability
    boundary, not parameter validation: the sandbox has no network, so search
    is the only door. If a docid the corpus contains were reachable without
    being retrieved, a program could walk the docid space and any recall
    measurement over it would be meaningless.
    """
    service = BrokerService({"local": FakeBackend("local")})
    service.register_session(make_session(backends=["local"], max_search_calls=5))
    await service.call("token", "search.local", {"query": "q"})

    with pytest.raises(ValueError, match="Unknown references"):
        await service.call("token", "content.get_many", {"refs": ["999"]})


async def test_offset_reaches_ranks_a_bare_limit_cannot() -> None:
    """Depth is authorisation, not convenience.

    Because a ref is minted only for a returned hit, `limit` is both what a
    program can see and what it is allowed to fetch. Without an offset, a
    document at rank 15 is not merely inconvenient to reach, it is unreachable.
    """
    service = BrokerService({"local": FakeBackend("local", depth=50)})
    state = service.register_session(make_session(backends=["local"], max_search_calls=5))

    shallow = await service.call("token", "search.local", {"query": "q", "limit": 10})
    deep = await service.call(
        "token", "search.local", {"query": "q", "limit": 10, "offset": 10}
    )

    assert [hit["rank"] for hit in shallow] == list(range(1, 11))
    # Ranks stay absolute: the second window reports 11..20, not 1..10 again.
    assert [hit["rank"] for hit in deep] == list(range(11, 21))
    assert not {hit["ref"] for hit in shallow} & {hit["ref"] for hit in deep}
    # And the deeper hits are now fetchable, which is the point.
    assert deep[0]["docid"] in state.by_docid


async def test_grep_line_numbers_are_read_offsets() -> None:
    """The two halves compose without character arithmetic.

    This is the same contract the function-calling profiles keep, and matching
    it is deliberate: it is the coordinate system the model already writes
    against, and a second convention here would be paid for in wrong offsets.
    """
    lines = [f"line {index}" for index in range(1, 41)]
    lines[24] = "the target phrase is here"

    class Paged:
        name = "local"

        async def search(self, query, *, limit, offset=0, domains=None):
            return [SearchHit(ref="", backend="local", docid="d1", snippet="s", rank=1)]

        async def content(self, hits, *, query=None):
            return [ContentSnippet(ref=hit.ref, text="\n".join(lines)) for hit in hits]

    service = BrokerService({"local": Paged()})
    service.register_session(make_session(backends=["local"], max_search_calls=5))
    hits = await service.call("token", "search.local", {"query": "q"})
    ref = hits[0]["ref"]

    matches = await service.call(
        "token", "content.grep", {"refs": [ref], "pattern": r"target \w+", "context": 1}
    )
    assert len(matches) == 1
    assert matches[0]["line"] == 25
    assert matches[0]["before"] == ["line 24"]
    assert matches[0]["after"] == ["line 26"]

    window = await service.call(
        "token", "content.read", {"refs": [ref], "offset": matches[0]["line"], "limit": 2}
    )
    assert window[0]["text"].splitlines()[0] == "the target phrase is here"
    assert window[0]["metadata"]["start_line"] == 25
    assert window[0]["metadata"]["total_lines"] == 40


async def test_read_reports_where_to_continue_and_where_to_stop() -> None:
    """`next_offset` is None at the end, so `while offset:` terminates."""

    class Doc:
        name = "local"

        async def search(self, query, *, limit, offset=0, domains=None):
            return [SearchHit(ref="", backend="local", docid="d1", snippet="s", rank=1)]

        async def content(self, hits, *, query=None):
            return [ContentSnippet(ref=hit.ref, text="\n".join("abcde")) for hit in hits]

    service = BrokerService({"local": Doc()})
    service.register_session(make_session(backends=["local"], max_search_calls=5))
    ref = (await service.call("token", "search.local", {"query": "q"}))[0]["ref"]

    head = await service.call("token", "content.read", {"refs": [ref], "limit": 3})
    assert head[0]["text"] == "a\nb\nc"
    assert head[0]["metadata"]["next_offset"] == 4

    tail = await service.call(
        "token", "content.read", {"refs": [ref], "offset": 4, "limit": 3}
    )
    assert tail[0]["text"] == "d\ne"
    assert tail[0]["metadata"]["next_offset"] is None


async def test_grep_falls_back_to_a_literal_search_for_a_bad_pattern() -> None:
    """A program that meant `C++ (lang)` should get matches, not a traceback."""

    class Doc:
        name = "local"

        async def search(self, query, *, limit, offset=0, domains=None):
            return [SearchHit(ref="", backend="local", docid="d1", snippet="s", rank=1)]

        async def content(self, hits, *, query=None):
            return [ContentSnippet(ref=hit.ref, text="written in C++ (1985)") for hit in hits]

    service = BrokerService({"local": Doc()})
    service.register_session(make_session(backends=["local"], max_search_calls=5))
    ref = (await service.call("token", "search.local", {"query": "q"}))[0]["ref"]

    matches = await service.call("token", "content.grep", {"refs": [ref], "pattern": "C++ ("})
    assert [match["line"] for match in matches] == [1]


class CountingBackend:
    """Records how often it was actually asked to retrieve a document."""

    name = "local"

    def __init__(self, *, fail: set[str] | None = None) -> None:
        self.fetched: list[str] = []
        self.fail = fail or set()

    async def search(self, query, *, limit, offset=0, domains=None):
        return [
            SearchHit(ref="", backend="local", docid=str(index), snippet="s", rank=index)
            for index in range(offset + 1, offset + limit + 1)
        ]

    async def content(self, hits, *, query=None):
        rows = []
        for hit in hits:
            self.fetched.append(hit.docid)
            if hit.docid in self.fail:
                rows.append(
                    ContentSnippet(
                        ref=hit.ref,
                        text="",
                        metadata={"docid": hit.docid, "fetch_error": "HTTPError: 403"},
                    )
                )
            else:
                rows.append(
                    ContentSnippet(
                        ref=hit.ref,
                        text=f"body of {hit.docid}",
                        metadata={"docid": hit.docid},
                    )
                )
        return rows


async def test_a_document_is_retrieved_once_per_session() -> None:
    """grep and read are meant to be used repeatedly over one pool.

    Without a cache the recommended survey/locate/verify shape refetches every
    candidate once per stage. Against a local index that is merely wasteful;
    against a metered scrape API it is three times the bill and the latency.
    """
    backend = CountingBackend()
    service = BrokerService({"local": backend})
    state = service.register_session(make_session(backends=["local"], max_search_calls=5))
    hits = await service.call("token", "search.local", {"query": "q", "limit": 3})
    refs = [hit["ref"] for hit in hits]

    await service.call("token", "content.get_many", {"refs": refs})
    await service.call("token", "content.grep", {"refs": refs, "pattern": "body"})
    await service.call("token", "content.read", {"refs": refs})

    assert backend.fetched == ["1", "2", "3"]
    # Both numbers are reported: one follows the program's behaviour, the other
    # follows the bill, and a cache is exactly what makes them diverge.
    assert state.policy.usage.content_fetches == 9
    assert state.policy.usage.content_backend_fetches == 3


async def test_a_selected_passage_never_overwrites_the_cached_document() -> None:
    """`content.snippets` rewrites the rows it is handed.

    If those rows were the cached objects, one call to `snippets` would replace
    the stored document with the passage it chose, and every later `read` of
    that document would silently be a read of that passage instead.
    """
    backend = CountingBackend()
    service = BrokerService({"local": backend})
    service.register_session(make_session(backends=["local"], max_search_calls=5))
    ref = (await service.call("token", "search.local", {"query": "q", "limit": 1}))[0]["ref"]

    await service.call(
        "token", "content.snippets", {"refs": [ref], "query": "body", "max_tokens_per_page": 1}
    )
    rows = await service.call("token", "content.get_many", {"refs": [ref]})

    assert rows[0]["text"] == "body of 1"


async def test_every_requested_document_comes_back_in_order() -> None:
    """A short list is never mistaken for a complete one.

    A dropped failure makes a partial result look whole: the program sees two
    pages where it asked for three and cannot learn which one is missing, and
    `read` on a page that failed to load becomes indistinguishable from `read`
    on a page that is empty.
    """
    backend = CountingBackend(fail={"2"})
    service = BrokerService({"local": backend})
    service.register_session(make_session(backends=["local"], max_search_calls=5))
    hits = await service.call("token", "search.local", {"query": "q", "limit": 3})
    refs = [hit["ref"] for hit in hits]

    rows = await service.call("token", "content.get_many", {"refs": refs})

    assert [row["ref"] for row in rows] == refs
    assert rows[1]["metadata"]["fetch_error"] == "HTTPError: 403"
    assert rows[1]["text"] == ""
    # A failure is not cached: a transient timeout must not be frozen for the
    # rest of the rollout.
    await service.call("token", "content.get_many", {"refs": refs})
    assert backend.fetched == ["1", "2", "3", "2"]


async def test_every_fetch_failing_is_raised_not_reported_as_empty_pages() -> None:
    backend = CountingBackend(fail={"1", "2"})
    service = BrokerService({"local": backend})
    service.register_session(make_session(backends=["local"], max_search_calls=5))
    hits = await service.call("token", "search.local", {"query": "q", "limit": 2})

    with pytest.raises(RuntimeError, match="All 2 document fetches failed"):
        await service.call(
            "token", "content.get_many", {"refs": [hit["ref"] for hit in hits]}
        )


async def test_read_is_bounded_by_characters_as_well_as_lines() -> None:
    """A line is a sentence in one corpus and a whole section in another."""

    class Fat:
        name = "local"

        async def search(self, query, *, limit, offset=0, domains=None):
            return [SearchHit(ref="", backend="local", docid="d1", snippet="s", rank=1)]

        async def content(self, hits, *, query=None):
            return [ContentSnippet(ref=hit.ref, text="\n".join(["x" * 500] * 20)) for hit in hits]

    service = BrokerService({"local": Fat()})
    service.register_session(make_session(backends=["local"], max_search_calls=5))
    ref = (await service.call("token", "search.local", {"query": "q"}))[0]["ref"]

    rows = await service.call(
        "token", "content.read", {"refs": [ref], "limit": 20, "max_chars": 1200}
    )
    assert len(rows[0]["text"]) <= 1200
    assert rows[0]["metadata"]["truncated_by_max_chars"] is True
    # Trimmed on a line boundary, so the reported end_line is a real one and a
    # follow-up read resumes where this one stopped.
    assert rows[0]["metadata"]["end_line"] == 2
    assert rows[0]["metadata"]["next_offset"] == 3


def test_canonical_url_folds_only_what_is_safe_to_fold() -> None:
    canonical = BrokerService._canonical_url
    assert canonical("HTTPS://Example.COM/a?utm_source=x&id=7#frag") == (
        "https://example.com/a?id=7"
    )
    # Order of surviving parameters must not decide identity.
    assert canonical("https://e.com/p?b=2&a=1") == canonical("https://e.com/p?a=1&b=2")
    # Paths are left alone: /a and /a/ can be different pages and nothing here
    # can prove otherwise.
    assert canonical("https://e.com/a") != canonical("https://e.com/a/")


async def test_trace_records_identity_and_rank_for_every_hit() -> None:
    """Rank and duplication cannot be recovered after the fact.

    A baseline that logged only `result_count` can never be asked afterwards
    whether ranking or duplicate candidates were the bottleneck -- which is
    exactly the question that decides whether a fusion/dedup layer is worth
    building.
    """
    service = BrokerService(
        {"web": RankedBackend(["https://example.com/a", "https://example.com/b"])}
    )
    service.register_session(make_session(max_search_calls=5))

    await service.call(
        "token",
        "search.web_many",
        {"queries": ["one", "two"], "limit_per_query": 2},
        execution_id="exec-hits",
    )
    event = service.take_trace("token", "exec-hits")[0]

    # A fan-out lands in one event, so per-query duplication is visible in it.
    assert len(event.hits) == 4
    assert [hit.rank for hit in event.hits] == [1, 2, 1, 2]
    assert len({hit.identity for hit in event.hits}) == 2
    assert event.hits[0].score == 1.0
    # Addresses yes, page text no.
    assert "snippet" not in event.model_dump_json()


async def test_batching_disabled_forces_one_item_per_call() -> None:
    """The switch bounds fan-out; it does not remove the method.

    Removing `*_many` outright would also remove structured extraction, since
    `llm.extract_many` has no singular form, and the arm would then be measuring
    two things at once.
    """
    service = BrokerService({"web": RankedBackend(["https://example.com/a"])})
    service.register_session(
        make_session(max_search_calls=5, mechanisms=Mechanisms(batching=False))
    )

    with pytest.raises(MechanismDisabled, match="at most one item"):
        await service.call(
            "token",
            "search.web_many",
            {"queries": ["one", "two"]},
            execution_id="exec-block",
        )
    batches = await service.call("token", "search.web_many", {"queries": ["one"]})
    assert len(batches[0]["hits"]) == 1

    # A blocked call is still an event: an arm that disables a capability wants
    # to know how often the model kept reaching for it.
    blocked = service.take_trace("token", "exec-block")[0]
    assert blocked.status == "error"
    assert blocked.error_type == "MechanismDisabled"


async def test_llm_subroutine_disabled_blocks_the_whole_capability_class() -> None:
    service, client = make_llm_service(max_llm_calls=5)
    service.sessions["token"].session.mechanisms = Mechanisms(llm_subroutine=False)

    with pytest.raises(MechanismDisabled, match="plain Python"):
        await service.call("token", "llm.complete", {"prompt": "plan"})
    assert client.calls == []
    # Blocked before the quota is touched, so the arm does not also change budget.
    assert service.sessions["token"].policy.usage.llm_calls == 0


async def test_context_decoupling_disabled_echoes_results_into_the_trace() -> None:
    """The arm that separates "can orchestrate" from "middle never reaches context".

    Same interface, same expressiveness -- only the results come back, so the
    caller can put them in the control model's conversation.
    """
    service = BrokerService({"web": RankedBackend(["https://example.com/a"])})
    service.register_session(
        make_session(mechanisms=Mechanisms(context_decoupling=False))
    )

    hits = await service.call(
        "token", "search.web", {"query": "q"}, execution_id="exec-echo"
    )
    event = service.take_trace("token", "exec-echo")[0]
    assert event.result_payload == hits
    assert event.result_payload_truncated is False


async def test_default_sessions_keep_results_out_of_the_trace() -> None:
    service = BrokerService({"web": RankedBackend(["https://example.com/a"])})
    service.register_session(make_session())
    await service.call("token", "search.web", {"query": "q"}, execution_id="exec-plain")
    event = service.take_trace("token", "exec-plain")[0]
    assert event.result_payload is None


async def test_oversized_payload_is_capped_and_says_so() -> None:
    service = BrokerService(
        {"web": RankedBackend([f"https://example.com/{index}" for index in range(50)])},
        max_context_payload_bytes=200,
    )
    service.register_session(
        make_session(mechanisms=Mechanisms(context_decoupling=False))
    )
    await service.call(
        "token", "search.web", {"query": "q", "limit": 50}, execution_id="exec-big"
    )
    event = service.take_trace("token", "exec-big")[0]
    assert event.result_payload_truncated is True
    assert len(event.result_payload) == 200


async def test_capability_methods_stay_in_step_with_the_handler_table() -> None:
    """CAPABILITY_METHODS drives the session manifest and so the skill text.

    A capability added on one side only is either invisible to the model or
    advertised to it without an implementation, and both cost a turn to find
    out. The assertion lives on the dispatch path, so any call exercises it.
    """
    service = BrokerService({"web": RankedBackend(["https://example.com/a"])})
    service.register_session(make_session())
    with pytest.raises(ValueError, match="Unsupported capability"):
        await service.call("token", "search.nope", {})


def test_capabilities_manifest_drops_only_what_is_disabled() -> None:
    assert Mechanisms().capabilities() == list(CAPABILITY_METHODS)
    without_llm = Mechanisms(llm_subroutine=False).capabilities()
    assert not any(method.startswith("llm.") for method in without_llm)
    assert "search.web_many" in without_llm
    # Batching bounds a call's width rather than removing it, so the method is
    # still reachable and must still be advertised.
    assert Mechanisms(batching=False).capabilities() == list(CAPABILITY_METHODS)
