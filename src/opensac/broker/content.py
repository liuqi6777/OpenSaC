from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import re
from typing import Any

from opensac._contracts import ContentPassage, ContentPassageReport, ContentSnippet, SearchHit
from opensac.backends.rerank.base import PassageReranker
from opensac.backends.search.base import SearchBackend
from opensac.broker.call_context import current_call
from opensac.broker.documents import document_identity, resolve_sources
from opensac.broker.passages import (
    PassageCandidate,
    normalize_document_text,
    prefilter_passage_candidates,
    score_passage_candidates,
    segment_passages,
    select_passage_candidates,
)
from opensac.broker.provider_execution import CapabilityProviderError, ProviderExecutor
from opensac.broker.session import BrokerSession, EvidenceRecord, FlightGroup
from opensac.models import EvidenceTraceRecord, PassageTraceRecord, ProviderAttemptRecord
from opensac.provider import ProviderRequestError


def _provider_attempts() -> list[ProviderAttemptRecord]:
    context = current_call()
    return context.provider_attempts if context is not None else []


class ContentCapabilities:
    """Fetch admitted documents and derive bounded, citable evidence."""

    def __init__(
        self,
        backends: dict[str, SearchBackend],
        providers: ProviderExecutor,
        *,
        passage_reranker: PassageReranker | None,
        passage_chunk_chars: int,
        passage_chunk_overlap_chars: int,
        passage_prefilter_limit: int,
        max_query_chars: int,
        max_evidence_chars: int,
        max_evidence_records: int,
        max_evidence_passage_bytes: int,
        max_sources_per_request: int,
        session_content_cache_bytes: int,
        backend_revision: str,
    ) -> None:
        self.backends = backends
        self.providers = providers
        self.passage_reranker = passage_reranker
        self.passage_chunk_chars = passage_chunk_chars
        self.passage_chunk_overlap_chars = passage_chunk_overlap_chars
        self.passage_prefilter_limit = passage_prefilter_limit
        self.max_search_query_chars = max_query_chars
        self.max_evidence_chars = max_evidence_chars
        self.max_evidence_records = max_evidence_records
        self.max_evidence_passage_bytes = max_evidence_passage_bytes
        self.max_content_sources_per_request = max_sources_per_request
        self.session_content_cache_bytes = session_content_cache_bytes
        self.backend_revision = backend_revision
        self.inflight_coalescing = providers.inflight_coalescing

    def _resolve_content_sources(self, state: BrokerSession, sources: Any) -> list[SearchHit]:
        if isinstance(sources, str):
            normalized = [sources]
        elif isinstance(sources, list):
            normalized = sources
        else:
            raise ValueError("content sources must be a list or a single source")
        if len(normalized) > self.max_content_sources_per_request:
            raise ValueError(
                f"content request contains {len(normalized)} sources, exceeding the "
                f"broker maximum of {self.max_content_sources_per_request}"
            )
        return resolve_sources(state, normalized)

    @staticmethod
    def _content_sources_argument(
        params: dict[str, Any],
        *,
        legacy_options: tuple[str, ...] = (),
    ) -> Any:
        legacy = [key for key in ("refs", *legacy_options) if key in params]
        if legacy:
            raise ValueError(
                f"Unsupported legacy content parameter(s): {', '.join(sorted(legacy))}"
            )
        if "sources" not in params:
            raise ValueError("content requests must provide sources")
        return params["sources"]

    async def resolve_citations(
        self,
        state: BrokerSession,
        params: dict[str, Any],
    ) -> list[dict[str, Any]]:
        legacy = [key for key in ("refs", "requests") if key in params]
        if legacy:
            raise ValueError(
                f"Unsupported legacy citation parameter(s): {', '.join(sorted(legacy))}"
            )
        if "citations" not in params:
            raise ValueError("citation requests must provide citations")
        requests = params["citations"]
        if not isinstance(requests, list):
            raise ValueError("citations must be a list")
        resolved: list[dict[str, Any]] = []
        for request in requests:
            if not isinstance(request, dict):
                raise ValueError("Each citation request must be an object")
            if set(request) == {"source"}:
                source = request["source"]
                if not isinstance(source, str) or not source:
                    raise ValueError("Citation source must be a non-empty string")
                hit = resolve_sources(state, [source])[0]
                resolved.append(self._citation_wire(hit, hit.snippet, "search_preview"))
                continue
            if set(request) != {"locator"}:
                raise ValueError("Citation requests contain exactly source or locator")
            locator = request["locator"]
            record = self._verify_evidence_locator(state, locator)
            source = state.source_by_identity.get(record.identity)
            hit = state.documents_by_source.get(source or "")
            if hit is None:
                raise RuntimeError("Evidence locator lost its admitted document")
            citation = self._citation_wire(hit, record.text, record.kind)
            citation["locator"] = locator
            resolved.append(citation)
        return resolved

    @staticmethod
    def _citation_wire(
        hit: SearchHit,
        evidence: str,
        evidence_kind: str,
    ) -> dict[str, Any]:
        return {
            "source": hit.source,
            "title": hit.title,
            "url": hit.url,
            "docid": hit.docid,
            "evidence": evidence,
            "evidence_kind": evidence_kind,
            "backend": hit.backend,
        }

    def _verify_evidence_locator(
        self,
        state: BrokerSession,
        locator: Any,
    ) -> EvidenceRecord:
        registered: EvidenceRecord | None = None

        def reject(message: str, code: str) -> None:
            self._record_evidence_trace(
                locator_id=locator if isinstance(locator, str) else None,
                action="validate",
                status="error",
                record=registered,
                error_code=code,
            )
            raise ValueError(message)

        if not isinstance(locator, str) or not locator or len(locator) > 128:
            reject("Evidence locator must be a non-empty bounded string", "invalid_locator")
        registered = state.evidence.get(locator)
        if registered is None:
            reject("Unknown evidence locator", "unknown_locator")
        assert registered is not None
        self._record_evidence_trace(
            locator_id=locator,
            action="validate",
            status="ok",
            record=registered,
        )
        return registered

    @staticmethod
    def _record_evidence_trace(
        *,
        locator_id: str | None,
        action: str,
        status: str,
        record: EvidenceRecord | None,
        error_code: str | None = None,
    ) -> None:
        context = current_call()
        if context is None:
            return
        context.evidence_records.append(
            EvidenceTraceRecord(
                locator_id=locator_id[:128] if locator_id else None,
                identity=record.identity if record else None,
                action=action,
                status=status,
                coordinates=dict(record.coordinates) if record else {},
                document_fingerprint=record.document_fingerprint if record else None,
                passage_fingerprint=record.passage_fingerprint if record else None,
                error_code=error_code,
            )
        )

    def _register_evidence(
        self,
        state: BrokerSession,
        *,
        identity: str,
        text: str,
        document_text: str,
        coordinates: dict[str, Any],
    ) -> tuple[str | None, dict[str, Any] | None]:
        if not text or len(text) > self.max_evidence_chars:
            return None, None
        document_fingerprint = hashlib.sha256(document_text.encode("utf-8")).hexdigest()
        passage_fingerprint = hashlib.sha256(text.encode("utf-8")).hexdigest()
        material = json.dumps(
            {
                "identity": identity,
                "kind": "selected_passage",
                "coordinates": coordinates,
                "document_fingerprint": document_fingerprint,
                "passage_fingerprint": passage_fingerprint,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        locator_id = (
            "evidence_"
            + hashlib.sha256(f"{state.session.token}\0{material}".encode()).hexdigest()[:24]
        )
        record = EvidenceRecord(
            identity=identity,
            kind="selected_passage",
            text=text,
            coordinates=dict(coordinates),
            document_fingerprint=document_fingerprint,
            passage_fingerprint=passage_fingerprint,
        )
        existing = state.evidence.get(locator_id)
        if existing is not None:
            if existing != record:
                self._record_evidence_trace(
                    locator_id=locator_id,
                    action="issue",
                    status="error",
                    record=existing,
                    error_code="evidence_locator_collision",
                )
                raise RuntimeError("Evidence locator collision detected")
            self._record_evidence_trace(
                locator_id=locator_id,
                action="issue",
                status="ok",
                record=existing,
            )
            return locator_id, None

        passage_bytes = len(text.encode("utf-8"))
        if (
            len(state.evidence) >= self.max_evidence_records
            or state.evidence_passage_bytes + passage_bytes > self.max_evidence_passage_bytes
        ):
            self._record_evidence_trace(
                locator_id=locator_id,
                action="issue",
                status="error",
                record=record,
                error_code="evidence_capacity_exhausted",
            )
            return None, {
                "code": "evidence_capacity_exhausted",
                "message": "The session evidence registry is full.",
                "retryable": False,
            }

        state.evidence[locator_id] = record
        state.evidence_passage_bytes += passage_bytes
        state.policy.set_evidence_usage(
            records=len(state.evidence),
            passage_bytes=state.evidence_passage_bytes,
        )
        self._record_evidence_trace(
            locator_id=locator_id,
            action="issue",
            status="ok",
            record=record,
        )
        return locator_id, None

    async def get_many(
        self,
        state: BrokerSession,
        params: dict[str, Any],
    ) -> list[dict[str, Any]]:
        sources = self._content_sources_argument(params)
        hits = self._resolve_content_sources(state, sources)
        return await self._fetch_content(state, hits, query=None)

    async def _rerank_passages(
        self,
        state: BrokerSession,
        query: str,
        candidates: list[PassageCandidate],
    ) -> tuple[str, list[tuple[PassageCandidate, float]]]:
        reranker = self.passage_reranker
        if reranker is None:
            return "lexical:bm25", [
                (candidate, candidate.lexical_score) for candidate in candidates
            ]
        if not candidates:
            return reranker.name, []

        async def request() -> Any:
            return await reranker.rerank(query, [candidate.text for candidate in candidates])

        results = await self.providers.run(
            state,
            backend=reranker,
            operation="web.rerank",
            request_indexes=list(range(len(candidates))),
            request_value={
                "ranker": reranker.name,
                "query": query,
                "passages": [
                    hashlib.sha256(candidate.text.encode("utf-8")).hexdigest()
                    for candidate in candidates
                ],
            },
            request=request,
            preflight=reranker.preflight,
        )
        scores: dict[int, float] = {}
        for result in results:
            index = getattr(result, "index", None)
            score = getattr(result, "score", None)
            if (
                isinstance(index, bool)
                or not isinstance(index, int)
                or index < 0
                or index >= len(candidates)
                or index in scores
                or isinstance(score, bool)
                or not isinstance(score, (int, float))
                or not math.isfinite(float(score))
            ):
                raise ValueError("passage reranker returned invalid indexed scores")
            scores[index] = float(score)
        if set(scores) != set(range(len(candidates))):
            raise ValueError("passage reranker returned an incomplete score set")
        return reranker.name, [
            (candidate, scores[index]) for index, candidate in enumerate(candidates)
        ]

    async def passages(
        self,
        state: BrokerSession,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        raw_query = params.get("query", "")
        if not isinstance(raw_query, str):
            raise ValueError("query must be a string")
        query = raw_query.strip()
        if not query:
            raise ValueError("query must not be empty")
        if len(query) > self.max_search_query_chars:
            raise ValueError(
                f"query has {len(query)} characters, exceeding the broker maximum "
                f"of {self.max_search_query_chars}"
            )
        raw_limit = params.get("limit", 20)
        raw_max_per_source = params.get("max_per_source", 3)
        if (
            isinstance(raw_limit, bool)
            or not isinstance(raw_limit, int)
            or isinstance(raw_max_per_source, bool)
            or not isinstance(raw_max_per_source, int)
        ):
            raise ValueError("limit and max_per_source must be integers")
        limit = raw_limit
        max_per_source = raw_max_per_source
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        if not 1 <= max_per_source <= 10:
            raise ValueError("max_per_source must be between 1 and 10")

        raw_sources = self._content_sources_argument(
            params,
            legacy_options=("max_per_ref",),
        )
        input_count = (
            1
            if isinstance(raw_sources, str)
            else len(raw_sources)
            if isinstance(raw_sources, list)
            else 0
        )
        hits = self._resolve_content_sources(state, raw_sources)
        unique: list[tuple[int, SearchHit]] = []
        leader_by_source: dict[str, int] = {}
        for input_index, hit in enumerate(hits):
            leader_index = leader_by_source.get(hit.source)
            if leader_index is None:
                leader_by_source[hit.source] = input_index
                unique.append((input_index, hit))
                continue
            fingerprint = self.providers.fingerprint({"source": hit.source})
            self.providers.record_deduplicated_request(
                request_index=input_index,
                leader_index=leader_index,
                request_fingerprint=fingerprint,
            )
        duplicate_count = len(hits) - len(unique)
        if duplicate_count:
            state.policy.record_deduplicated(duplicate_count)
        if not unique:
            return ContentPassageReport(
                query=query,
                input_count=input_count,
                unique_source_count=0,
            ).model_dump(mode="json")

        rows = await self._fetch_content(
            state,
            [hit for _, hit in unique],
            query=None,
        )
        failures: list[dict[str, Any]] = []
        candidates: list[PassageCandidate] = []
        normalized_documents: dict[str, str] = {}
        for (input_index, hit), row in zip(unique, rows, strict=True):
            failure = row.get("failure")
            if failure is not None:
                failures.append(
                    {
                        "input_index": input_index,
                        "source": hit.source,
                        "failure": failure,
                    }
                )
                continue
            document_text = normalize_document_text(str(row.get("text") or ""))
            normalized_documents[hit.source] = document_text
            for text, start, end, coordinates in segment_passages(
                document_text,
                chunk_chars=self.passage_chunk_chars,
                overlap_chars=self.passage_chunk_overlap_chars,
            ):
                candidates.append(
                    PassageCandidate(
                        hit=hit,
                        input_index=input_index,
                        title=str(row.get("title") or hit.title or ""),
                        url=row.get("url") or hit.url,
                        date=row.get("date") or hit.date,
                        text=text,
                        start=start,
                        end=end,
                        coordinates=coordinates,
                    )
                )

        retained = prefilter_passage_candidates(
            score_passage_candidates(query, candidates),
            max_per_source=max_per_source,
            limit=self.passage_prefilter_limit,
        )
        ranker_name, reranked = await self._rerank_passages(state, query, retained)
        selected = select_passage_candidates(
            reranked,
            max_per_source=max_per_source,
            limit=limit,
        )

        passages: list[dict[str, Any]] = []
        context = current_call()
        traced = context.passage_records if context is not None else None
        for rank, (candidate, score) in enumerate(selected, start=1):
            coordinates = candidate.coordinates.model_dump(mode="json")
            locator, locator_error = self._register_evidence(
                state,
                identity=document_identity(candidate.hit),
                text=candidate.text,
                document_text=normalized_documents[candidate.hit.source],
                coordinates={
                    "type": "line_characters",
                    "basis": "normalized_text",
                    **coordinates,
                },
            )
            row = ContentPassage(
                source=candidate.hit.source,
                title=candidate.title,
                date=candidate.date,
                text=candidate.text,
                coordinates=candidate.coordinates,
                rank=rank,
                score=score,
                ranker=ranker_name,
                locator=locator,
                locator_error=locator_error,
            ).model_dump(mode="json")
            passages.append(row)
            if traced is not None:
                traced.append(
                    PassageTraceRecord(
                        identity=document_identity(candidate.hit),
                        ranker=ranker_name,
                        rank=rank,
                        score=score,
                        coordinates=coordinates,
                        passage_fingerprint=hashlib.sha256(
                            candidate.text.encode("utf-8")
                        ).hexdigest(),
                    )
                )
        return ContentPassageReport(
            query=query,
            passages=passages,
            failures=failures,
            input_count=input_count,
            unique_source_count=len(unique),
        ).model_dump(mode="json")

    # `read` and `grep_report` share a 1-indexed line contract: a match line is
    # directly usable as a read offset. Both operate on normalized backend text
    # and remain independent of the selected search provider.

    @staticmethod
    def _document_lines(row: dict[str, Any]) -> list[str]:
        return str(row.get("text") or "").splitlines()

    async def read(
        self,
        state: BrokerSession,
        params: dict[str, Any],
    ) -> list[dict[str, Any]]:
        sources = self._content_sources_argument(params)
        hits = self._resolve_content_sources(state, sources)
        rows = await self._fetch_content(state, hits, query=None)
        # 1-indexed, and an offset below 1 is clamped rather than refused: a
        # program computing `match.line - 5` near the top of a document is
        # asking for the beginning, not making an error.
        offset = max(int(params.get("offset", 1)), 1)
        limit = min(max(int(params.get("limit", 200)), 1), 5_000)
        # A line is not a fixed amount of text. In the local corpus a line is a
        # sentence; in a scraped web page it is often a whole section, so the
        # same `limit` spans two orders of magnitude between backends. This is
        # a ceiling on the response, not a budget the program is meant to
        # manage -- generous enough that ordinary reading never meets it.
        max_chars = min(max(int(params.get("max_chars", 100_000)), 1), 400_000)
        windows: list[dict[str, Any]] = []
        for hit, row in zip(hits, rows, strict=True):
            document_text = str(row.get("text") or "")
            lines = self._document_lines(row)
            total = len(lines)
            window = lines[offset - 1 : offset - 1 + limit]
            # Trim by whole lines, so `end_line` keeps meaning what it says and
            # a follow-up read resumes on a real boundary.
            clipped = False
            while window and len("\n".join(window)) > max_chars and len(window) > 1:
                window.pop()
                clipped = True
            text = "\n".join(window)
            partial_line = len(window) == 1 and len(text) > max_chars
            if partial_line:
                text = text[:max_chars]
                clipped = True
            end = offset - 1 + len(window)
            metadata = {
                **row.get("metadata", {}),
                "start_line": offset if window else 0,
                "end_line": end,
                "total_lines": total,
                # None at end of document, so `while next_offset:` is a correct
                # scroll loop.
                "next_offset": end + 1 if end < total else None,
            }
            if clipped:
                metadata["truncated_by_max_chars"] = True
            if partial_line:
                # The public read coordinate is line-based, so a single line
                # cannot be resumed mid-line. Report the partial window
                # explicitly and bind its locator with character coordinates
                # instead of claiming the prefix represents the whole line.
                metadata["truncated_mid_line"] = True
                metadata["partial_line_remaining_chars"] = len(window[0]) - len(text)
            result = {**row, "text": text, "metadata": metadata}
            coordinates = (
                {
                    "type": "line_characters",
                    "line": metadata["start_line"],
                    "start_character": 0,
                    "end_character": len(text),
                }
                if partial_line
                else {
                    "type": "lines",
                    "start_line": metadata["start_line"],
                    "end_line": metadata["end_line"],
                }
            )
            locator, locator_error = self._register_evidence(
                state,
                identity=document_identity(hit),
                text=text,
                document_text=document_text,
                coordinates=coordinates,
            )
            if locator is not None:
                result["locator"] = locator
            if locator_error is not None:
                result["locator_error"] = locator_error
            windows.append(result)
        return windows

    @staticmethod
    def _compile_pattern(pattern: str) -> re.Pattern[str]:
        """Case-insensitive, and a malformed regex degrades to a literal search.

        A program that meant to search for ``C++ (programming)`` should get its
        matches rather than a traceback about an unbalanced parenthesis.
        """
        try:
            return re.compile(pattern, flags=re.IGNORECASE)
        except re.error:
            return re.compile(re.escape(pattern), flags=re.IGNORECASE)

    async def grep_report(
        self,
        state: BrokerSession,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        pattern = str(params.get("pattern", ""))
        if not pattern:
            raise ValueError("pattern must not be empty")
        sources = self._content_sources_argument(
            params,
            legacy_options=("max_matches_per_ref",),
        )
        hits = self._resolve_content_sources(state, sources)
        rows = await self._fetch_content(
            state,
            hits,
            query=None,
            raise_on_all_failures=False,
        )
        regex = self._compile_pattern(pattern)
        context = min(max(int(params.get("context", 0)), 0), 20)
        # Bounded per document rather than in total: an unbounded grep over 50
        # candidates is how a program fills its own output budget with one call,
        # and a global cap would let the first document starve the other 49.
        max_per_source = min(max(int(params.get("max_matches_per_source", 20)), 1), 200)
        matches: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        for input_index, (hit, row) in enumerate(zip(hits, rows, strict=True)):
            failure = row.get("failure")
            if failure is None and row.get("metadata", {}).get("fetch_error"):
                failure = {
                    "code": "provider_rejected",
                    "message": "Provider rejected one document.",
                    "retryable": False,
                    "attempts": 1,
                }
            if failure is not None:
                failures.append(
                    {
                        "input_index": input_index,
                        "source": row.get("source", ""),
                        "failure": failure,
                    }
                )
                continue
            lines = self._document_lines(row)
            found = 0
            for index, line in enumerate(lines):
                if found >= max_per_source:
                    break
                if not regex.search(line):
                    continue
                found += 1
                before = lines[max(0, index - context) : index] if context else []
                after = lines[index + 1 : index + 1 + context] if context else []
                match = {
                    "source": row.get("source", ""),
                    "title": row.get("title", ""),
                    "line": index + 1,
                    "text": line,
                    "before": before,
                    "after": after,
                    "input_index": input_index,
                }
                evidence = "\n".join([*before, line, *after])
                locator, locator_error = self._register_evidence(
                    state,
                    identity=document_identity(hit),
                    text=evidence,
                    document_text=str(row.get("text") or ""),
                    coordinates={
                        "type": "lines",
                        "start_line": index + 1 - len(before),
                        "end_line": index + 1 + len(after),
                        "match_line": index + 1,
                    },
                )
                if locator is not None:
                    match["locator"] = locator
                if locator_error is not None:
                    match["locator_error"] = locator_error
                matches.append(match)
        return {
            "matches": matches,
            "failures": failures,
            "input_count": 1 if isinstance(sources, str) else len(sources),
        }

    async def _fetch_content(
        self,
        state: BrokerSession,
        hits: list[SearchHit],
        *,
        query: str | None,
        raise_on_all_failures: bool = False,
    ) -> list[dict[str, Any]]:
        """Text for every requested hit, in the order requested.

        Three properties the callers above depend on. One row per hit, so a
        program can pair results with what it asked for. Caller order, so
        pairing is positional and not a join. And a document already read in
        this session is served from the cache: `grep` and `read` exist to be
        used repeatedly over one pool, and fetching it again per stage is
        affordable against a local index but is three times the bill and the
        latency against a paid scrape API.
        """
        keys: list[str] = []
        leaders: dict[str, tuple[int, SearchHit]] = {}
        for index, hit in enumerate(hits):
            state.policy.require_backend(hit.backend)
            fingerprint = self.providers.fingerprint(
                {
                    "backend": hit.backend,
                    "revision": self.backend_revision,
                    "identity": document_identity(hit),
                }
            )
            keys.append(fingerprint)
            leader = leaders.get(fingerprint)
            if leader is None:
                leaders[fingerprint] = (index, hit)
                continue
            self.providers.record_deduplicated_request(
                request_index=index,
                leader_index=leader[0],
                request_fingerprint=fingerprint,
            )

        duplicate_count = len(hits) - len(leaders)
        if duplicate_count:
            state.policy.record_deduplicated(duplicate_count)
        misses = {
            key: value
            for key, value in leaders.items()
            if document_identity(value[1]) not in state.content_cache
        }
        await state.policy.record_content_fetches(len(hits), 0)
        # The logical-usage reservation above may yield while another caller's
        # flight completes. Its transport leader writes the cache before
        # removing that flight, so refreshing here closes the only window in
        # which this call could admit a second leader for an already-cached
        # document.
        misses = {
            key: value
            for key, value in misses.items()
            if document_identity(value[1]) not in state.content_cache
        }

        async def fetch_one(
            key: str,
            input_index: int,
            hit: SearchHit,
            *,
            operation_id: str | None = None,
            track_execution: bool = True,
        ) -> tuple[str, dict[str, Any]]:
            backend = self.backends.get(hit.backend)
            if backend is None:
                return key, self._content_failure_row(
                    hit,
                    {
                        "code": "provider_not_configured",
                        "message": f"Backend '{hit.backend}' is not configured.",
                        "retryable": False,
                        "attempts": 0,
                    },
                )
            operation = "local.document" if hit.backend == "local" else "web.scrape"

            async def request() -> ContentSnippet:
                fetch = getattr(backend, "fetch", None)
                if not callable(fetch):
                    raise ValueError("backend has no atomic content fetch operation")
                result = await fetch(hit, query=query)
                row = ContentSnippet.model_validate(result)
                if row.source != hit.source:
                    raise ValueError("backend changed the requested content source")
                return row

            validate_fetch = getattr(backend, "preflight_fetch", None)

            def preflight() -> None:
                if callable(validate_fetch):
                    validate_fetch(hit)
                # This counter represents admitted unique provider leaders,
                # not logical input rows. Keeping it in the runtime preflight
                # means an invalid handle or missing credential is rejected
                # before either this usage or a governor token is consumed.
                state.policy.record_content_backend_fetches(1)

            try:
                snippet = await self.providers.run(
                    state,
                    backend=backend,
                    operation=operation,
                    request_indexes=[input_index],
                    request_value={
                        "backend": hit.backend,
                        "revision": self.backend_revision,
                        "identity": document_identity(hit),
                    },
                    request=request,
                    preflight=preflight,
                    operation_id=operation_id,
                    track_execution=track_execution,
                )
            except ProviderRequestError as exc:
                return key, self._content_failure_row(
                    hit,
                    self.providers.provider_failure(exc),
                )

            row = snippet.model_dump(mode="json", exclude={"url"})
            metadata = dict(row.get("metadata") or {})
            for metadata_key in ("ref", "url", "docid", "source"):
                metadata.pop(metadata_key, None)
            row["metadata"] = metadata
            legacy_error = row.get("metadata", {}).get("fetch_error")
            if row.get("failure") is None and legacy_error:
                attempts = max(
                    (
                        record.attempt
                        for record in _provider_attempts()
                        if input_index in record.request_indexes and record.operation == operation
                    ),
                    default=1,
                )
                return key, self._content_failure_row(
                    hit,
                    {
                        "code": "provider_rejected",
                        "message": "Provider rejected one document.",
                        "retryable": False,
                        "attempts": attempts,
                    },
                )
            if row.get("date") is None and hit.date is not None:
                row["date"] = hit.date
            return key, row

        if self.inflight_coalescing and misses:
            flight_key_for_fingerprint = {
                fingerprint: self.providers.flight_key(
                    "local.document" if hit.backend == "local" else "web.scrape",
                    fingerprint,
                )
                for fingerprint, (_index, hit) in misses.items()
            }
            admission = await self.providers.admit_flights(
                state,
                {
                    flight_key_for_fingerprint[fingerprint]: (
                        fingerprint,
                        [input_index],
                    )
                    for fingerprint, (input_index, _hit) in misses.items()
                },
                group_new=False,
            )
            fingerprint_for_flight_key = {
                flight_key: fingerprint
                for fingerprint, flight_key in flight_key_for_fingerprint.items()
            }
            for group in admission.new_groups:
                if len(group.keys) != 1:
                    raise RuntimeError("content flight contains multiple keys")
                flight_key = next(iter(group.keys))
                fingerprint = fingerprint_for_flight_key[flight_key]
                input_index, hit = misses[fingerprint]
                identity = document_identity(hit)
                cached = state.content_cache.get(identity)
                if cached is not None:

                    async def execute_cached_content(
                        flight_key: str = flight_key,
                        cached: dict[str, Any] = cached,
                    ) -> dict[str, dict[str, Any]]:
                        # Cache and flight admission are separate structures.
                        # A previous leader may populate the cache while this
                        # call waits for the flight lock; publish that row
                        # through the newly admitted future without starting a
                        # second provider operation.
                        return {flight_key: copy.deepcopy(cached)}

                    self.providers.start_flight_group(state, group, execute_cached_content)
                    continue

                async def execute_content(
                    group: FlightGroup = group,
                    flight_key: str = flight_key,
                    fingerprint: str = fingerprint,
                    input_index: int = input_index,
                    hit: SearchHit = hit,
                ) -> dict[str, dict[str, Any]]:
                    _key, row = await fetch_one(
                        fingerprint,
                        input_index,
                        hit,
                        operation_id=group.operation_id,
                        track_execution=False,
                    )
                    # Publish successful content to the session before the
                    # flight runner removes its active key or resolves waiter
                    # futures. A caller queued behind flight cleanup therefore
                    # observes either the active flight or the cache, never a
                    # gap between the two.
                    state.cache_content(
                        document_identity(hit),
                        row,
                        self.session_content_cache_bytes,
                    )
                    return {flight_key: row}

                self.providers.start_flight_group(state, group, execute_content)

            returned_rows = await asyncio.gather(
                *(
                    self.providers.await_flight(
                        state,
                        admission.waiters[flight_key_for_fingerprint[fingerprint]],
                    )
                    for fingerprint in misses
                )
            )
            fetched = dict(zip(misses, returned_rows, strict=True))
        else:
            returned = await asyncio.gather(
                *(fetch_one(key, input_index, hit) for key, (input_index, hit) in misses.items())
            )
            fetched = dict(returned)
        for fingerprint, row in fetched.items():
            _input_index, hit = misses[fingerprint]
            state.cache_content(
                document_identity(hit),
                row,
                self.session_content_cache_bytes,
            )

        rows: list[dict[str, Any]] = []
        for key, hit in zip(keys, hits, strict=True):
            stored = state.content_cache.get(document_identity(hit)) or fetched.get(key)
            if stored is None:
                stored = self._content_failure_row(
                    hit,
                    {
                        "code": "provider_invalid_response",
                        "message": "Provider returned no result for this document.",
                        "retryable": False,
                        "attempts": 0,
                    },
                )
            row = copy.deepcopy(stored)
            if row.get("date") is None and hit.date is not None:
                row["date"] = hit.date
            rows.append(row)

        failures = [row["failure"] for row in rows if row.get("failure") is not None]
        all_failed = bool(rows) and len(failures) == len(rows)
        all_systemic = all_failed and all(
            self.providers.is_systemic_content_failure(failure) for failure in failures
        )
        if all_systemic or (raise_on_all_failures and all_failed):
            raise CapabilityProviderError.from_failures(
                failures,
                attempts=len(_provider_attempts()),
            )
        return rows

    @staticmethod
    def _content_failure_row(
        hit: SearchHit,
        failure: dict[str, Any],
    ) -> dict[str, Any]:
        return ContentSnippet(
            source=hit.source,
            text="",
            url=hit.url,
            title=hit.title,
            date=hit.date,
            failure=failure,
            metadata={"backend": hit.backend},
        ).model_dump(mode="json", exclude={"url"})
