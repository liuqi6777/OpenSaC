from __future__ import annotations

import asyncio
import copy
import hashlib
import math
import re
from typing import Any
from urllib.parse import urlsplit

from opensac._contracts import (
    ContentGrepReport,
    ContentPassage,
    ContentPassageReport,
    ContentReadWindow,
    ContentSnippet,
    SearchHit,
)
from opensac.backends.document import document_fetch_candidates
from opensac.backends.rerank.base import PassageReranker
from opensac.backends.search.base import SearchBackend
from opensac.broker.call_context import current_call
from opensac.broker.session import BrokerSession, FlightGroup
from opensac.broker.validation import boolean, integer, string
from opensac.models import HitRecord, PassageTraceRecord, ProviderAttemptRecord
from opensac.provider import ProviderRequestError

from ..providers.execution import ProviderExecutor
from .documents import document_identity, normalize_source, public_web_url
from .passages import (
    PassageCandidate,
    normalize_document_text,
    prefilter_passage_candidates,
    score_passage_candidates,
    segment_passages,
    select_passage_candidates,
)


def _provider_attempts() -> list[ProviderAttemptRecord]:
    context = current_call()
    return context.provider_attempts if context is not None else []


class ContentCapabilities:
    """Fetch admitted documents and derive bounded passages."""

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
        max_sources_per_request: int,
        session_content_cache_bytes: int,
        content_url_admission: str,
        content_batch_deadline_seconds: float,
        backend_revision: str,
    ) -> None:
        self.backends = backends
        self.providers = providers
        self.passage_reranker = passage_reranker
        self.passage_chunk_chars = passage_chunk_chars
        self.passage_chunk_overlap_chars = passage_chunk_overlap_chars
        self.passage_prefilter_limit = passage_prefilter_limit
        self.max_search_query_chars = max_query_chars
        self.max_content_sources_per_request = max_sources_per_request
        self.session_content_cache_bytes = session_content_cache_bytes
        if content_url_admission not in {"searched_only", "searched_or_public_web"}:
            raise ValueError("content_url_admission is invalid")
        self.content_url_admission = content_url_admission
        if float(content_batch_deadline_seconds) <= 0:
            raise ValueError("content_batch_deadline_seconds must be positive")
        self.content_batch_deadline_seconds = float(content_batch_deadline_seconds)
        self.backend_revision = backend_revision
        self.inflight_coalescing = providers.flights.enabled

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
        hits: list[SearchHit] = []
        backend_names = sorted(state.policy.allowed_backends & set(self.backends))
        if len(backend_names) != 1:
            raise RuntimeError("A session must have exactly one configured search backend")
        backend_name = backend_names[0]
        for input_index, raw_source in enumerate(normalized):
            if not isinstance(raw_source, str):
                raise ValueError(f"content source at input index {input_index} must be a string")
            try:
                source = normalize_source(raw_source)
            except ValueError as exc:
                raise ValueError(
                    f"content source at input index {input_index} is invalid: {exc}"
                ) from exc
            record = state.document_for_alias(source)
            if record is not None:
                hit = record.hit.model_copy(deep=True)
                hit.source = raw_source.strip()
                hits.append(hit)
                continue

            try:
                web_source = public_web_url(source)
            except ValueError as exc:
                hits.append(
                    SearchHit(
                        source=source,
                        backend=backend_name,
                        url=(
                            source
                            if backend_name == "web"
                            and urlsplit(source).scheme.lower() in {"http", "https"}
                            and bool(urlsplit(source).netloc)
                            else None
                        ),
                        docid=(
                            source
                            if backend_name == "local"
                            or urlsplit(source).scheme.lower() not in {"http", "https"}
                            or not urlsplit(source).netloc
                            else None
                        ),
                        rank=0,
                        metadata={
                            "_opensac_admission_failure": {
                                "code": "unknown_source",
                                "message": str(exc),
                                "retryable": False,
                                "attempts": 0,
                            }
                        },
                    )
                )
                continue

            if backend_name != "web" or self.content_url_admission == "searched_only":
                hits.append(
                    SearchHit(
                        source=source,
                        backend=backend_name,
                        url=web_source,
                        rank=0,
                        metadata={
                            "_opensac_admission_failure": {
                                "code": "url_not_admitted",
                                "message": (
                                    "This deployment only reads web URLs admitted by search."
                                ),
                                "retryable": False,
                                "attempts": 0,
                            }
                        },
                    )
                )
                continue

            hits.append(
                SearchHit(
                    source=raw_source.strip(),
                    backend="web",
                    url=web_source,
                    domain=urlsplit(web_source).hostname,
                    rank=0,
                    metadata={"_opensac_direct_url": True},
                )
            )
            state.policy.record_direct_url_attempt()

        context = current_call()
        if context is not None:
            for hit in hits:
                registered = state.document_for_alias(normalize_source(hit.source))
                admission = (
                    "direct_url"
                    if hit.metadata.get("_opensac_direct_url")
                    else registered.admission
                    if registered is not None
                    else None
                )
                context.hits.append(
                    HitRecord(
                        identity=document_identity(hit),
                        rank=hit.rank,
                        score=hit.score,
                        admission=admission,
                    )
                )
        return hits

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

    @staticmethod
    def _content_source_argument(params: dict[str, Any]) -> str:
        if "sources" in params:
            raise ValueError("content.read uses singular source; use read_many for batches")
        if "source" not in params:
            raise ValueError("content.read must provide source")
        source = params["source"]
        if not isinstance(source, str):
            raise ValueError("content source must be a string")
        return source

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
    ) -> tuple[str, list[tuple[PassageCandidate, float]], list[dict[str, Any]]]:
        reranker = self.passage_reranker
        if reranker is None:
            return (
                "lexical:bm25",
                [(candidate, candidate.lexical_score) for candidate in candidates],
                [],
            )
        if not candidates:
            return reranker.name, [], []

        def lexical_fallback(
            failure: dict[str, Any],
        ) -> tuple[
            str,
            list[tuple[PassageCandidate, float]],
            list[dict[str, Any]],
        ]:
            return (
                "lexical:bm25",
                [(candidate, candidate.lexical_score) for candidate in candidates],
                [failure],
            )

        async def request() -> Any:
            return await reranker.rerank(query, [candidate.text for candidate in candidates])

        attempts_before = len(_provider_attempts())
        try:
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
        except ProviderRequestError as exc:
            return lexical_fallback(self.providers.provider_failure(exc))
        rerank_attempts = len(_provider_attempts()) - attempts_before
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
                return lexical_fallback(
                    self.providers.contextualize_failure(
                        {
                            "code": "provider_invalid_response",
                            "message": "Passage reranker returned invalid indexed scores.",
                            "retryable": False,
                            "attempts": rerank_attempts,
                        },
                        backend=reranker,
                        operation="web.rerank",
                    )
                )
            scores[index] = float(score)
        if set(scores) != set(range(len(candidates))):
            return lexical_fallback(
                self.providers.contextualize_failure(
                    {
                        "code": "provider_invalid_response",
                        "message": "Passage reranker returned an incomplete score set.",
                        "retryable": False,
                        "attempts": rerank_attempts,
                    },
                    backend=reranker,
                    operation="web.rerank",
                )
            )
        return (
            reranker.name,
            [(candidate, scores[index]) for index, candidate in enumerate(candidates)],
            [],
        )

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
        ranker_name, reranked, rerank_warnings = await self._rerank_passages(state, query, retained)
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
            row = ContentPassage(
                source=candidate.hit.source,
                title=candidate.title,
                date=candidate.date,
                text=candidate.text,
                coordinates=candidate.coordinates,
                rank=rank,
                score=score,
                ranker=ranker_name,
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
            warnings=rerank_warnings,
            input_count=input_count,
            unique_source_count=len(unique),
        ).model_dump(mode="json")

    # `read`, `read_many`, and `grep` share a 1-indexed line contract: a match line is
    # directly usable as a read offset. Both operate on normalized backend text
    # and remain independent of the selected search provider.

    @staticmethod
    def _document_lines(row: dict[str, Any]) -> list[str]:
        return str(row.get("text") or "").splitlines()

    async def read(
        self,
        state: BrokerSession,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        source = self._content_source_argument(params)
        offset = integer(params.get("offset", 1), "offset", minimum=1)
        limit = integer(params.get("limit", 200), "limit", minimum=1, maximum=5_000)
        max_chars = integer(
            params.get("max_chars", 100_000),
            "max_chars",
            minimum=1,
            maximum=400_000,
        )
        hits = self._resolve_content_sources(state, [source])
        rows = await self._fetch_content(state, hits, query=None)
        return self._slice_content_row(
            rows[0],
            offset=offset,
            limit=limit,
            max_chars=max_chars,
        )

    async def read_many(
        self,
        state: BrokerSession,
        params: dict[str, Any],
    ) -> list[dict[str, Any]]:
        raw_windows = params.get("windows")
        if not isinstance(raw_windows, list):
            raise ValueError("windows must be a list")
        if len(raw_windows) > self.max_content_sources_per_request:
            raise ValueError(
                f"content request contains {len(raw_windows)} windows, exceeding the "
                f"broker maximum of {self.max_content_sources_per_request}"
            )
        windows = [ContentReadWindow.model_validate(window) for window in raw_windows]
        hits = self._resolve_content_sources(state, [window.source for window in windows])
        rows = await self._fetch_content(state, hits, query=None)
        return [
            {
                **self._slice_content_row(
                    row,
                    offset=window.offset,
                    limit=window.limit,
                    max_chars=window.max_chars,
                ),
                "input_index": input_index,
            }
            for input_index, (window, row) in enumerate(zip(windows, rows, strict=True))
        ]

    def _slice_content_row(
        self,
        row: dict[str, Any],
        *,
        offset: int,
        limit: int,
        max_chars: int,
    ) -> dict[str, Any]:
        lines = self._document_lines(row)
        total = len(lines)
        window = lines[offset - 1 : offset - 1 + limit]
        # A line can be much larger than the line count suggests. Trim whole
        # lines first so end_line remains a resumable coordinate.
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
            "next_offset": end + 1 if end < total else None,
        }
        if clipped:
            metadata["truncated_by_max_chars"] = True
        if partial_line:
            metadata["truncated_mid_line"] = True
            metadata["partial_line_remaining_chars"] = len(window[0]) - len(text)
        return {**row, "text": text, "metadata": metadata}

    @staticmethod
    def _compile_pattern(
        pattern: str,
        *,
        mode: str,
        case_sensitive: bool,
    ) -> re.Pattern[str]:
        flags = 0 if case_sensitive else re.IGNORECASE
        expression = pattern if mode == "regex" else re.escape(pattern)
        try:
            return re.compile(expression, flags=flags)
        except re.error as exc:
            raise ValueError(f"pattern is not a valid regular expression: {exc}") from None

    async def grep(
        self,
        state: BrokerSession,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        pattern = string(params.get("pattern", ""), "pattern", max_chars=4_096)
        mode = params.get("mode", "regex")
        if mode not in {"regex", "literal"}:
            raise ValueError("mode must be 'regex' or 'literal'")
        case_sensitive = boolean(params.get("case_sensitive", False), "case_sensitive")
        context = integer(params.get("context", 0), "context", minimum=0, maximum=20)
        max_per_source = integer(
            params.get("max_matches_per_source", 20),
            "max_matches_per_source",
            minimum=1,
            maximum=200,
        )
        regex = self._compile_pattern(
            pattern,
            mode=mode,
            case_sensitive=case_sensitive,
        )
        sources = self._content_sources_argument(
            params,
            legacy_options=("max_matches_per_ref",),
        )
        hits = self._resolve_content_sources(state, sources)
        rows = await self._fetch_content(
            state,
            hits,
            query=None,
        )
        matches: list[dict[str, Any]] = []
        source_results: list[dict[str, Any]] = []
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
                source_results.append(
                    {
                        "input_index": input_index,
                        "source": row.get("source") or hit.source,
                        "title": row.get("title") or hit.title or "",
                        "match_count": 0,
                        "scan_complete": False,
                        "failure": failure,
                    }
                )
                continue
            lines = self._document_lines(row)
            found = 0
            scan_complete = True
            for index, line in enumerate(lines):
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
                matches.append(match)
                if found >= max_per_source and index < len(lines) - 1:
                    scan_complete = False
                    break
            source_results.append(
                {
                    "input_index": input_index,
                    "source": row.get("source") or hit.source,
                    "title": row.get("title") or hit.title or "",
                    "match_count": found,
                    "scan_complete": scan_complete,
                    "failure": None,
                }
            )
        return ContentGrepReport(
            pattern=pattern,
            mode=mode,
            case_sensitive=case_sensitive,
            context=context,
            max_matches_per_source=max_per_source,
            matches=matches,
            source_results=source_results,
            input_count=1 if isinstance(sources, str) else len(sources),
        ).model_dump(mode="json")

    async def _fetch_content(
        self,
        state: BrokerSession,
        hits: list[SearchHit],
        *,
        query: str | None,
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
            admission_failure = hit.metadata.get("_opensac_admission_failure")
            if isinstance(admission_failure, dict):
                return key, self._content_failure_row(hit, admission_failure)
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
            validate_fetch = getattr(backend, "preflight_fetch", None)
            candidates = document_fetch_candidates(hit) if hit.backend == "web" else [hit]
            total_attempts = 0
            row: dict[str, Any] | None = None
            fallback_codes = {
                "provider_rejected",
                "provider_not_found",
                "provider_invalid_response",
            }
            for candidate_index, candidate in enumerate(candidates):

                async def request(candidate: SearchHit = candidate) -> ContentSnippet:
                    fetch = getattr(backend, "fetch", None)
                    if not callable(fetch):
                        raise ValueError("backend has no atomic content fetch operation")
                    result = await fetch(candidate, query=query)
                    snippet = ContentSnippet.model_validate(result)
                    if snippet.source != hit.source:
                        raise ValueError("backend changed the requested content source")
                    return snippet

                def preflight(candidate: SearchHit = candidate) -> None:
                    if callable(validate_fetch):
                        validate_fetch(candidate)
                    # Every representation attempt is separately governed and accounted.
                    state.policy.record_content_backend_fetches(1)

                candidate_operation_id = (
                    f"{operation_id}:{candidate_index}" if operation_id is not None else None
                )
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
                            "representation": candidate.metadata.get(
                                "_opensac_representation", "original"
                            ),
                        },
                        request=request,
                        preflight=preflight,
                        operation_id=candidate_operation_id,
                        track_execution=track_execution,
                    )
                except ProviderRequestError as exc:
                    failure = self.providers.provider_failure(exc)
                    total_attempts += int(failure.get("attempts") or 0)
                    has_fallback = candidate_index + 1 < len(candidates)
                    if has_fallback and failure["code"] in fallback_codes:
                        continue
                    failure["attempts"] = total_attempts
                    return key, self._content_failure_row(hit, failure)

                row = snippet.model_dump(mode="json", exclude={"url"})
                metadata = dict(row.get("metadata") or {})
                for metadata_key in ("ref", "url", "docid", "source"):
                    metadata.pop(metadata_key, None)
                representation = candidate.metadata.get("_opensac_representation")
                if representation:
                    metadata["representation"] = representation
                row["metadata"] = metadata
                legacy_error = metadata.get("fetch_error")
                if row.get("failure") is None and legacy_error:
                    attempts = max(
                        (
                            record.attempt
                            for record in _provider_attempts()
                            if input_index in record.request_indexes
                            and record.operation == operation
                        ),
                        default=1,
                    )
                    total_attempts = max(total_attempts, attempts)
                    if candidate_index + 1 < len(candidates):
                        continue
                    return key, self._content_failure_row(
                        hit,
                        {
                            "code": "provider_rejected",
                            "message": "Provider rejected one document.",
                            "retryable": False,
                            "attempts": total_attempts,
                        },
                    )
                break

            if row is None:
                return key, self._content_failure_row(
                    hit,
                    {
                        "code": "provider_invalid_response",
                        "message": "Provider returned no document representation.",
                        "retryable": False,
                        "attempts": total_attempts,
                    },
                )
            if row.get("date") is None and hit.date is not None:
                row["date"] = hit.date
            identity = document_identity(hit)
            if hit.metadata.get("_opensac_direct_url"):
                registered_hit = hit.model_copy(deep=True)
                registered_hit.metadata.pop("_opensac_direct_url", None)
                candidate_source = normalize_source(hit.source)
                aliases = {candidate_source}
                if hit.url:
                    aliases.add(normalize_source(hit.url))
                state.remember(
                    registered_hit,
                    identity=identity,
                    candidate_source=candidate_source,
                    admission="direct_url",
                    aliases=aliases,
                )
                state.policy.record_direct_url_success()
            state.mark_fetched(identity)
            return key, row

        async def collect_bounded(
            tasks: dict[str, asyncio.Task[tuple[str, dict[str, Any]]]],
        ) -> dict[str, dict[str, Any]]:
            if not tasks:
                return {}
            done, pending = await asyncio.wait(
                set(tasks.values()),
                timeout=self.content_batch_deadline_seconds,
            )
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            results: dict[str, dict[str, Any]] = {}
            for fingerprint, task in tasks.items():
                if task not in done:
                    input_index, hit = misses[fingerprint]
                    operation = "local.document" if hit.backend == "local" else "web.scrape"
                    attempts = sum(
                        1
                        for record in _provider_attempts()
                        if input_index in record.request_indexes and record.operation == operation
                    )
                    results[fingerprint] = self._content_failure_row(
                        hit,
                        {
                            "code": "content_deadline_exceeded",
                            "message": "The content batch deadline was exceeded.",
                            "retryable": True,
                            "attempts": attempts,
                        },
                    )
                    continue
                returned_key, row = task.result()
                results[returned_key] = row
            return results

        if self.inflight_coalescing and misses:
            flight_key_for_fingerprint = {
                fingerprint: self.providers.flights.key(
                    "local.document" if hit.backend == "local" else "web.scrape",
                    fingerprint,
                )
                for fingerprint, (_index, hit) in misses.items()
            }
            admission = await self.providers.flights.admit(
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

                    self.providers.flights.start(state, group, execute_cached_content)
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

                self.providers.flights.start(state, group, execute_content)

            async def await_content_flight(
                fingerprint: str,
            ) -> tuple[str, dict[str, Any]]:
                row = await self.providers.flights.wait(
                    state,
                    admission.waiters[flight_key_for_fingerprint[fingerprint]],
                )
                return fingerprint, row

            fetched = await collect_bounded(
                {
                    fingerprint: asyncio.create_task(await_content_flight(fingerprint))
                    for fingerprint in misses
                }
            )
        else:
            fetched = await collect_bounded(
                {
                    key: asyncio.create_task(fetch_one(key, input_index, hit))
                    for key, (input_index, hit) in misses.items()
                }
            )
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
            row["source"] = hit.source
            if row.get("date") is None and hit.date is not None:
                row["date"] = hit.date
            rows.append(row)

        return rows

    def _content_failure_row(
        self,
        hit: SearchHit,
        failure: dict[str, Any],
    ) -> dict[str, Any]:
        operation = "local.document" if hit.backend == "local" else "web.scrape"
        failure = self.providers.contextualize_failure(
            failure,
            backend=self.backends.get(hit.backend),
            operation=operation,
        )
        return ContentSnippet(
            source=hit.source,
            text="",
            url=hit.url,
            title=hit.title,
            date=hit.date,
            failure=failure,
            metadata={"backend": hit.backend},
        ).model_dump(mode="json", exclude={"url"})
