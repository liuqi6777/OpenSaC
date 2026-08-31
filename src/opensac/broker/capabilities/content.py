from __future__ import annotations

import asyncio
import copy
import hashlib
import re
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from opensac.backends.document import DocumentBackend, DocumentContent, DocumentHandle
from opensac.backends.rerank import RerankScore, TextReranker, bm25_scores
from opensac.broker._utils import (
    document_identity,
    integer,
    normalize_source,
    normalize_web_source,
    public_web_url,
    string,
)
from opensac.broker.call_context import current_call, current_provider_attempts
from opensac.broker.failures import CapabilityFailure
from opensac.broker.registry import BaseCapabilities, CapabilityRequest, capability_method
from opensac.broker.session import BrokerSession, FlightGroup
from opensac.provider import ProviderRequestError, invalid_provider_response
from opensac.tracing import HitRecord, PassageTraceRecord

from ..providers.execution import BackendBinding, CapabilityProviderError, ProviderExecutor
from .passages import (
    PassageCandidate,
    PassageCoordinates,
    normalize_document_text,
    prefilter_passage_candidates,
    score_passage_prefilter,
    segment_passages,
    select_passage_candidates,
)
from .search import SearchLimits

if TYPE_CHECKING:
    from opensac.broker.capabilities.catalog import CapabilityBuildContext

type ContentSources = str | list[str]

_DOCUMENT_HANDLES = TypeAdapter(list[DocumentHandle])
_RERANK_SCORES = TypeAdapter(list[RerankScore])


class ContentLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    max_sources_per_request: int = Field(default=256, ge=1)
    url_admission: Literal["searched_only", "searched_or_public_web"] = "searched_or_public_web"
    batch_deadline_seconds: float = Field(default=60.0, gt=0.0)
    session_cache_bytes: int = Field(default=32_000_000, ge=0)

    read_max_line_count: int = Field(default=5_000, ge=1)
    read_max_chars: int = Field(default=400_000, ge=1)
    grep_max_pattern_chars: int = Field(default=4_096, ge=1)
    grep_max_context_lines: int = Field(default=20, ge=0)
    grep_max_limit_per_source: int = Field(default=200, ge=1)
    passage_limit: int = Field(default=100, ge=1)
    passage_limit_per_source: int = Field(default=10, ge=1)

    passage_chunk_chars: int = Field(default=2_000, ge=1)
    passage_chunk_overlap_chars: int = Field(default=200, ge=0)
    passage_prefilter_limit: int = Field(default=100, ge=1, le=100)

    @model_validator(mode="after")
    def validate_passage_window(self) -> Self:
        if self.passage_chunk_overlap_chars >= self.passage_chunk_chars:
            raise ValueError("passage_chunk_overlap_chars must be smaller than chunk size")
        return self


class _ContentRequest(CapabilityRequest):
    @staticmethod
    def reject_legacy(data: Any, *names: str) -> Any:
        if not isinstance(data, dict):
            return data
        legacy = sorted(name for name in names if name in data)
        if legacy:
            raise ValueError(f"Unsupported legacy content parameter(s): {', '.join(legacy)}")
        return data


class ContentFetchRequest(_ContentRequest):
    source: str

    @model_validator(mode="before")
    @classmethod
    def reject_removed_parameters(cls, data: Any) -> Any:
        if isinstance(data, dict) and "sources" in data:
            raise ValueError("this content operation accepts one source")
        return cls.reject_legacy(data, "refs")


class ContentPassagesRequest(_ContentRequest):
    query: str = ""
    sources: ContentSources
    limit: int = 20
    limit_per_source: int = 3

    @model_validator(mode="before")
    @classmethod
    def reject_removed_parameters(cls, data: Any) -> Any:
        return cls.reject_legacy(data, "refs", "max_per_ref")


class ContentReadRequest(_ContentRequest):
    source: str
    start_line: int = 1
    start_character: int = 0
    line_count: int = 200
    max_chars: int = 100_000

    @model_validator(mode="before")
    @classmethod
    def reject_removed_parameters(cls, data: Any) -> Any:
        if isinstance(data, dict) and "sources" in data:
            raise ValueError("this content operation accepts one source")
        return cls.reject_legacy(data, "refs")


class ContentGrepRequest(_ContentRequest):
    sources: ContentSources
    pattern: str = ""
    mode: str = "regex"
    case_sensitive: bool = False
    start_line: int = 1
    context_lines: int = 0
    limit_per_source: int = 20

    @model_validator(mode="before")
    @classmethod
    def reject_removed_parameters(cls, data: Any) -> Any:
        return cls.reject_legacy(
            data,
            "refs",
            "max_matches_per_ref",
            "context",
            "max_matches_per_source",
        )


class ContentDocument(BaseModel):
    """One successfully fetched document at the public capability boundary."""

    model_config = ConfigDict(extra="forbid")

    source: str = Field(min_length=1, max_length=4_096)
    text: str
    title: str = ""
    date: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ContentResult(ContentDocument):
    """One successful item in an input-indexed content report."""

    input_index: int = Field(ge=0)


class ContentFailure(CapabilityFailure):
    """One failed source in an input-indexed content report."""

    input_index: int = Field(ge=0)
    source: str = Field(min_length=1, max_length=4_096)


class ContentCursor(BaseModel):
    """Exact location of the next unread character in normalized content."""

    model_config = ConfigDict(extra="forbid")

    start_line: int = Field(ge=1)
    start_character: int = Field(ge=0)


class ContentWindow(BaseModel):
    """Coordinates describing one bounded content slice."""

    model_config = ConfigDict(extra="forbid")

    start_line: int | None = Field(default=None, ge=1)
    start_character: int = Field(ge=0)
    end_line: int | None = Field(default=None, ge=1)
    end_character: int = Field(ge=0)
    total_lines: int = Field(ge=0)
    next: ContentCursor | None = None
    truncated_by_max_chars: bool


class ContentSlice(ContentDocument):
    """One read window with provider metadata kept separate from coordinates."""

    window: ContentWindow


class ContentMatchSpan(BaseModel):
    """One 0-based, end-exclusive match span within a line."""

    model_config = ConfigDict(extra="forbid")

    start_character: int = Field(ge=0)
    end_character: int = Field(ge=0)


class ContentMatch(BaseModel):
    """One matching document line with exact read coordinates."""

    model_config = ConfigDict(extra="forbid")

    source: str = Field(min_length=1, max_length=4_096)
    title: str = ""
    line: int = Field(ge=1)
    text: str
    before: list[str] = Field(default_factory=list)
    after: list[str] = Field(default_factory=list)
    spans: list[ContentMatchSpan] = Field(min_length=1)
    input_index: int = Field(ge=0)


class ContentGrepSourceResult(BaseModel):
    """One successful source scan."""

    model_config = ConfigDict(extra="forbid")

    input_index: int = Field(ge=0)
    source: str = Field(min_length=1, max_length=4_096)
    title: str = ""
    match_count: int = Field(ge=0)
    scan_complete: bool
    next_start_line: int | None = Field(default=None, ge=1)


class ContentGrepReport(BaseModel):
    """Flat matches plus separate successful scans and fetch failures."""

    model_config = ConfigDict(extra="forbid")

    pattern: str = Field(min_length=1, max_length=4_096)
    mode: Literal["regex", "literal"]
    case_sensitive: bool
    start_line: int = Field(ge=1)
    context_lines: int = Field(ge=0, le=20)
    limit_per_source: int = Field(ge=1, le=200)
    matches: list[ContentMatch] = Field(default_factory=list)
    source_results: list[ContentGrepSourceResult] = Field(default_factory=list)
    failures: list[ContentFailure] = Field(default_factory=list)
    input_count: int = Field(ge=0)

    @model_validator(mode="after")
    def _validate_alignment(self) -> Self:
        indexes = [row.input_index for row in self.source_results]
        indexes.extend(row.input_index for row in self.failures)
        if sorted(indexes) != list(range(self.input_count)):
            raise ValueError("grep results and failures must partition the input indexes")

        source_results = {row.input_index: row for row in self.source_results}
        counts = Counter(match.input_index for match in self.matches)
        for match in self.matches:
            source_result = source_results.get(match.input_index)
            if source_result is None or match.source != source_result.source:
                raise ValueError("each match must belong to a successful source result")
        for row in self.source_results:
            if row.match_count != counts[row.input_index]:
                raise ValueError("source result match_count does not match flat matches")
            if not row.scan_complete and row.match_count != self.limit_per_source:
                raise ValueError("an incomplete scan must have reached the per-source limit")
            if row.scan_complete != (row.next_start_line is None):
                raise ValueError("grep continuation must agree with scan_complete")
        return self


class ContentPassage(BaseModel):
    """One globally ranked passage from a caller-authorized source set."""

    model_config = ConfigDict(extra="forbid")

    source: str = Field(min_length=1, max_length=4_096)
    title: str = ""
    date: str | None = None
    text: str = Field(min_length=1)
    coordinates: PassageCoordinates
    rank: int = Field(ge=1)
    score: float = Field(allow_inf_nan=False)
    ranker: str = Field(min_length=1)


class ContentPassageReport(BaseModel):
    """Ranked passages plus flat fetch failures and reranker warnings."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1)
    passages: list[ContentPassage] = Field(default_factory=list)
    failures: list[ContentFailure] = Field(default_factory=list)
    warnings: list[CapabilityFailure] = Field(default_factory=list)
    input_count: int = Field(ge=0)
    unique_source_count: int = Field(ge=0)

    @model_validator(mode="after")
    def _validate_counts(self) -> Self:
        if self.unique_source_count > self.input_count:
            raise ValueError("unique_source_count cannot exceed input_count")
        return self


@dataclass(frozen=True, slots=True)
class _ResolvedSource:
    """One content input after broker admission, independent of search results."""

    input_index: int
    route: str
    handle: DocumentHandle
    admission: Literal["search", "direct_url"] | None = None
    rank: int = 0
    score: float | None = None
    register_on_success: bool = False


type _SourceOutcome = _ResolvedSource | ContentFailure
type _FetchOutcome = ContentDocument | ContentFailure


class ContentCapabilities(BaseCapabilities):
    """Fetch admitted documents and derive bounded passages."""

    name = "content"
    available = True

    def __init__(
        self,
        providers: ProviderExecutor,
        document_bindings: dict[str, BackendBinding[DocumentBackend]],
        *,
        rerank_binding: BackendBinding[TextReranker],
        limits: ContentLimits,
        search_limits: SearchLimits,
    ) -> None:
        if not document_bindings:
            raise ValueError("at least one document backend must be configured")
        self.providers = providers
        self.document_bindings = document_bindings
        self.rerank_binding = rerank_binding
        self.limits = limits
        self.passage_chunk_chars = limits.passage_chunk_chars
        self.passage_chunk_overlap_chars = limits.passage_chunk_overlap_chars
        self.passage_prefilter_limit = limits.passage_prefilter_limit
        self.max_search_query_chars = search_limits.max_query_chars
        self.max_content_sources_per_request = limits.max_sources_per_request
        self.session_content_cache_bytes = limits.session_cache_bytes
        self.content_url_admission = limits.url_admission
        self.content_batch_deadline_seconds = limits.batch_deadline_seconds
        self.inflight_coalescing = providers.flights.enabled

    @classmethod
    def from_context(cls, context: CapabilityBuildContext) -> Self:
        return cls(
            context.providers,
            context.document_bindings,
            rerank_binding=context.rerank_binding,
            limits=context.config.content,
            search_limits=context.config.search,
        )

    def _passage_queries(self, request: ContentPassagesRequest) -> list[str]:
        query = request.query
        return [query[: self.max_search_query_chars]] if query else []

    @staticmethod
    def _source_count(request: ContentPassagesRequest | ContentGrepRequest) -> int:
        return 1 if isinstance(request.sources, str) else len(request.sources)

    @staticmethod
    def _passage_count(result: Any) -> int:
        return len(result.get("passages", [])) if isinstance(result, dict) else 0

    @staticmethod
    def _match_count(result: Any) -> int:
        return len(result.get("matches", [])) if isinstance(result, dict) else 0

    def manifest(self, *, backend_name: str) -> dict[str, Any]:
        if backend_name not in self.document_bindings:
            raise ValueError(f"Backend {backend_name!r} is not configured for content")
        return {
            "url_admission": self.limits.url_admission,
            "limits": {
                "max_sources_per_request": self.limits.max_sources_per_request,
                "read_max_line_count": self.limits.read_max_line_count,
                "read_max_chars": self.limits.read_max_chars,
                "grep_max_context_lines": self.limits.grep_max_context_lines,
                "grep_max_limit_per_source": self.limits.grep_max_limit_per_source,
                "passage_limit": self.limits.passage_limit,
                "passage_limit_per_source": self.limits.passage_limit_per_source,
            },
        }

    def _document_binding(
        self,
        state: BrokerSession,
    ) -> tuple[str, BackendBinding[DocumentBackend]]:
        backend_names = sorted(state.policy.allowed_backends & set(self.document_bindings))
        if len(backend_names) != 1:
            raise RuntimeError("A session must have exactly one configured document backend")
        backend_name = backend_names[0]
        return backend_name, self.document_bindings[backend_name]

    def _contextualize_document_failure(
        self,
        binding: BackendBinding[DocumentBackend],
        failure: dict[str, Any],
    ) -> dict[str, Any]:
        return self.providers.contextualize_failure(
            failure,
            backend=binding.backend,
            component=binding.component,
            resource_failures=binding.resource_failures,
        )

    def _resolve_content_sources(
        self,
        state: BrokerSession,
        sources: Any,
    ) -> list[_SourceOutcome]:
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
        resolved: list[_SourceOutcome] = []
        backend_name, binding = self._document_binding(state)
        accepts_public_urls = binding.backend.source_kind == "public_url"

        def failure(input_index: int, source: str, code: str, message: str) -> ContentFailure:
            detail = self._contextualize_document_failure(
                binding,
                {
                    "code": code,
                    "message": message,
                    "retryable": False,
                    "attempts": 0,
                },
            )
            return ContentFailure(
                input_index=input_index,
                source=source,
                **detail,
            )

        context = current_call()

        def trace(
            route: str,
            handle: DocumentHandle,
            *,
            rank: int = 0,
            score: float | None = None,
            admission: Literal["search", "direct_url"] | None = None,
        ) -> None:
            if context is None:
                return
            context.hits.append(
                HitRecord(
                    identity=document_identity(route, handle),
                    rank=rank,
                    score=score,
                    admission=admission,
                )
            )

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
            if record is None and accepts_public_urls:
                try:
                    source = normalize_web_source(raw_source)
                except ValueError as exc:
                    raise ValueError(
                        f"content source at input index {input_index} is invalid: {exc}"
                    ) from exc
                record = state.document_for_alias(source)
            if record is not None:
                handle = record.handle.model_copy(update={"source": raw_source.strip()})
                resolved.append(
                    _ResolvedSource(
                        input_index=input_index,
                        route=record.route,
                        handle=handle,
                        admission=record.admission,
                        rank=record.rank,
                        score=record.score,
                    )
                )
                trace(
                    record.route,
                    handle,
                    rank=record.rank,
                    score=record.score,
                    admission=record.admission,
                )
                continue

            try:
                web_source = public_web_url(source)
            except ValueError as exc:
                try:
                    parts = urlsplit(source)
                except ValueError:
                    absolute_web_url = False
                else:
                    absolute_web_url = (
                        accepts_public_urls
                        and parts.scheme.lower() in {"http", "https"}
                        and bool(parts.netloc)
                    )
                handle = DocumentHandle(
                    source=source,
                    url=source if absolute_web_url else None,
                    docid=source if not absolute_web_url else None,
                )
                resolved.append(failure(input_index, source, "unknown_source", str(exc)))
                trace(backend_name, handle)
                continue

            if not accepts_public_urls or self.content_url_admission == "searched_only":
                handle = DocumentHandle(source=source, url=web_source)
                resolved.append(
                    failure(
                        input_index,
                        source,
                        "url_not_admitted",
                        "This deployment only reads web URLs admitted by search.",
                    )
                )
                trace(backend_name, handle)
                continue

            handle = DocumentHandle(source=raw_source.strip(), url=web_source)
            resolved.append(
                _ResolvedSource(
                    input_index=input_index,
                    route=backend_name,
                    handle=handle,
                    admission="direct_url",
                    register_on_success=True,
                )
            )
            state.policy.record_direct_url_attempt()
            trace(backend_name, handle, admission="direct_url")
        return resolved

    @capability_method("content.fetch", ContentFetchRequest)
    async def fetch(
        self,
        state: BrokerSession,
        request: ContentFetchRequest,
    ) -> dict[str, Any]:
        resolved = self._resolve_content_sources(state, [request.source])
        outcomes = await self._fetch_content(state, resolved, query=None)
        outcome = outcomes[0]
        if isinstance(outcome, ContentFailure):
            raise CapabilityProviderError.from_failure(
                outcome.model_dump(mode="json", exclude={"input_index", "source"}),
                attempts=outcome.attempts,
            )
        return outcome.model_dump(mode="json", exclude={"input_index"})

    async def _rerank_passages(
        self,
        state: BrokerSession,
        query: str,
        candidates: list[PassageCandidate],
    ) -> tuple[str, list[tuple[PassageCandidate, float]], list[dict[str, Any]]]:
        binding = self.rerank_binding
        ranker_name = binding.backend.name
        if not candidates:
            return ranker_name, [], []

        def lexical_fallback(
            failure: dict[str, Any],
        ) -> tuple[
            str,
            list[tuple[PassageCandidate, float]],
            list[dict[str, Any]],
        ]:
            scores = bm25_scores(query, [candidate.text for candidate in candidates])
            return (
                "lexical:bm25",
                list(zip(candidates, scores, strict=True)),
                [failure],
            )

        try:
            scores = await self._score_reranker(
                state,
                query,
                [candidate.text for candidate in candidates],
            )
        except ProviderRequestError as exc:
            return lexical_fallback(self.providers.provider_failure(exc))
        return (
            ranker_name,
            list(zip(candidates, scores, strict=True)),
            [],
        )

    async def _score_reranker(
        self,
        state: BrokerSession,
        query: str,
        texts: list[str],
    ) -> list[float]:
        if not texts:
            return []
        binding = self.rerank_binding

        async def rerank(backend: TextReranker) -> list[float]:
            results = _RERANK_SCORES.validate_python(
                await backend.rerank(query, texts),
                strict=True,
            )
            indexed_scores: dict[int, float] = {}
            for result in results:
                if result.index >= len(texts) or result.index in indexed_scores:
                    raise self._invalid_rerank_response("Reranker returned invalid indexed scores.")
                indexed_scores[result.index] = result.score
            if set(indexed_scores) != set(range(len(texts))):
                raise self._invalid_rerank_response("Reranker returned an incomplete score set.")
            return [indexed_scores[index] for index in range(len(texts))]

        return await self.providers.execute(
            state,
            binding,
            request_indexes=list(range(len(texts))),
            request_value={
                "ranker": binding.backend.name,
                "query": query,
                "items": [hashlib.sha256(text.encode("utf-8")).hexdigest() for text in texts],
            },
            request=rerank,
            preflight=binding.backend.preflight,
        )

    @staticmethod
    def _invalid_rerank_response(message: str) -> ProviderRequestError:
        return ProviderRequestError(
            "provider_invalid_response",
            message,
            retryable=False,
        )

    @capability_method(
        "content.passages",
        ContentPassagesRequest,
        trace_queries="_passage_queries",
        trace_input_count="_source_count",
        trace_result_count="_passage_count",
    )
    async def passages(
        self,
        state: BrokerSession,
        request: ContentPassagesRequest,
    ) -> dict[str, Any]:
        query = request.query.strip()
        if not query:
            raise ValueError("query must not be empty")
        if len(query) > self.max_search_query_chars:
            raise ValueError(
                f"query has {len(query)} characters, exceeding the broker maximum "
                f"of {self.max_search_query_chars}"
            )
        limit = request.limit
        limit_per_source = request.limit_per_source
        if not 1 <= limit <= self.limits.passage_limit:
            raise ValueError(f"limit must be between 1 and {self.limits.passage_limit}")
        if not 1 <= limit_per_source <= self.limits.passage_limit_per_source:
            raise ValueError(
                f"limit_per_source must be between 1 and {self.limits.passage_limit_per_source}"
            )

        raw_sources = request.sources
        input_count = (
            1
            if isinstance(raw_sources, str)
            else len(raw_sources)
            if isinstance(raw_sources, list)
            else 0
        )
        resolved_sources = self._resolve_content_sources(state, raw_sources)
        unique: list[_SourceOutcome] = []
        leader_by_source: dict[str, int] = {}
        for item in resolved_sources:
            source = item.handle.source if isinstance(item, _ResolvedSource) else item.source
            input_index = item.input_index
            leader_index = leader_by_source.get(source)
            if leader_index is None:
                leader_by_source[source] = input_index
                unique.append(item)
                continue
            fingerprint = self.providers.fingerprint({"source": source})
            self.providers.record_deduplicated_request(
                request_index=input_index,
                leader_index=leader_index,
                request_fingerprint=fingerprint,
            )
        duplicate_count = len(resolved_sources) - len(unique)
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
            unique,
            query=None,
        )
        failures: list[ContentFailure] = []
        candidates: list[PassageCandidate] = []
        for item, row in zip(unique, rows, strict=True):
            if isinstance(row, ContentFailure):
                failures.append(row)
                continue
            if not isinstance(item, _ResolvedSource):
                raise RuntimeError("Successful content outcome has no resolved source.")
            document_text = normalize_document_text(row.text)
            for text, start, end, coordinates in segment_passages(
                document_text,
                chunk_chars=self.passage_chunk_chars,
                overlap_chars=self.passage_chunk_overlap_chars,
            ):
                candidates.append(
                    PassageCandidate(
                        route=item.route,
                        handle=item.handle,
                        input_index=item.input_index,
                        title=row.title or item.handle.title,
                        url=item.handle.url,
                        date=row.date or item.handle.date,
                        text=text,
                        start=start,
                        end=end,
                        coordinates=coordinates,
                    )
                )

        retained = prefilter_passage_candidates(
            score_passage_prefilter(query, candidates),
            limit_per_source=limit_per_source,
            limit=self.passage_prefilter_limit,
        )
        ranker_name, reranked, rerank_warnings = await self._rerank_passages(state, query, retained)
        selected = select_passage_candidates(
            reranked,
            limit_per_source=limit_per_source,
            limit=limit,
        )

        passages: list[dict[str, Any]] = []
        context = current_call()
        traced = context.passage_records if context is not None else None
        for rank, (candidate, score) in enumerate(selected, start=1):
            coordinates = candidate.coordinates.model_dump(mode="json")
            row = ContentPassage(
                source=candidate.handle.source,
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
                        identity=document_identity(candidate.route, candidate.handle),
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

    # `read` and `grep` share a 1-indexed line contract. Character positions are
    # 0-based and end-exclusive, matching passage coordinates.

    @staticmethod
    def _document_lines(document: ContentDocument) -> list[str]:
        text = normalize_document_text(document.text)
        return [] if not text else text.split("\n")

    @capability_method("content.read", ContentReadRequest)
    async def read(
        self,
        state: BrokerSession,
        request: ContentReadRequest,
    ) -> dict[str, Any]:
        start_line = integer(request.start_line, "start_line", minimum=1)
        start_character = integer(
            request.start_character,
            "start_character",
            minimum=0,
        )
        line_count = integer(
            request.line_count,
            "line_count",
            minimum=1,
            maximum=self.limits.read_max_line_count,
        )
        max_chars = integer(
            request.max_chars,
            "max_chars",
            minimum=1,
            maximum=self.limits.read_max_chars,
        )
        resolved = self._resolve_content_sources(state, [request.source])
        outcomes = await self._fetch_content(state, resolved, query=None)
        outcome = outcomes[0]
        if isinstance(outcome, ContentFailure):
            raise CapabilityProviderError.from_failure(
                outcome.model_dump(mode="json", exclude={"input_index", "source"}),
                attempts=outcome.attempts,
            )
        return self._slice_content_document(
            outcome,
            start_line=start_line,
            start_character=start_character,
            line_count=line_count,
            max_chars=max_chars,
        ).model_dump(mode="json")

    def _slice_content_document(
        self,
        document: ContentDocument,
        *,
        start_line: int,
        start_character: int,
        line_count: int,
        max_chars: int,
    ) -> ContentSlice:
        lines = self._document_lines(document)
        total_lines = len(lines)
        if start_line > total_lines:
            if start_character:
                raise ValueError("start_character must be 0 when start_line is past EOF")
            return ContentSlice(
                source=document.source,
                text="",
                title=document.title,
                date=document.date,
                metadata=document.metadata,
                window=ContentWindow(
                    start_line=None,
                    start_character=0,
                    end_line=None,
                    end_character=0,
                    total_lines=total_lines,
                    next=None,
                    truncated_by_max_chars=False,
                ),
            )

        first_index = start_line - 1
        if start_character > len(lines[first_index]):
            raise ValueError("start_character exceeds the length of start_line")

        stop_index = min(total_lines, first_index + line_count)
        pieces: list[str] = []
        remaining = max_chars
        current_index = first_index
        character = start_character
        end_line: int | None = None
        end_character = 0
        next_cursor: ContentCursor | None = None
        truncated = False

        while current_index < stop_index:
            line = lines[current_index]
            segment = line[character:]
            separator = "\n" if current_index > first_index else ""
            required = len(separator) + len(segment)
            if required <= remaining:
                pieces.append(separator + segment)
                remaining -= required
                end_line = current_index + 1
                end_character = len(line)
                current_index += 1
                character = 0
                continue

            truncated = True
            if separator and not remaining:
                next_cursor = ContentCursor(
                    start_line=current_index,
                    start_character=len(lines[current_index - 1]),
                )
                break
            if separator and remaining:
                pieces.append(separator)
                remaining -= 1
                end_line = current_index + 1
                end_character = 0
            take = min(len(segment), remaining)
            if take:
                pieces.append(segment[:take])
                end_line = current_index + 1
                end_character = character + take
            if character + take < len(line):
                next_cursor = ContentCursor(
                    start_line=current_index + 1,
                    start_character=character + take,
                )
            elif current_index + 1 < total_lines:
                next_cursor = ContentCursor(
                    start_line=current_index + 2,
                    start_character=0,
                )
            break

        if not truncated and current_index < total_lines:
            if remaining:
                pieces.append("\n")
                end_line = current_index + 1
                end_character = 0
                if current_index == total_lines - 1 and not lines[current_index]:
                    next_cursor = None
                else:
                    next_cursor = ContentCursor(
                        start_line=current_index + 1,
                        start_character=0,
                    )
            else:
                truncated = True
                next_cursor = ContentCursor(
                    start_line=current_index,
                    start_character=len(lines[current_index - 1]),
                )

        return ContentSlice(
            source=document.source,
            text="".join(pieces),
            title=document.title,
            date=document.date,
            metadata=document.metadata,
            window=ContentWindow(
                start_line=start_line,
                start_character=start_character,
                end_line=end_line,
                end_character=end_character,
                total_lines=total_lines,
                next=next_cursor,
                truncated_by_max_chars=truncated,
            ),
        )

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

    @capability_method(
        "content.grep",
        ContentGrepRequest,
        trace_input_count="_source_count",
        trace_result_count="_match_count",
    )
    async def grep(
        self,
        state: BrokerSession,
        request: ContentGrepRequest,
    ) -> dict[str, Any]:
        pattern = string(
            request.pattern,
            "pattern",
            max_chars=self.limits.grep_max_pattern_chars,
        )
        mode = request.mode
        if mode not in {"regex", "literal"}:
            raise ValueError("mode must be 'regex' or 'literal'")
        case_sensitive = request.case_sensitive
        start_line = integer(request.start_line, "start_line", minimum=1)
        context_lines = integer(
            request.context_lines,
            "context_lines",
            minimum=0,
            maximum=self.limits.grep_max_context_lines,
        )
        limit_per_source = integer(
            request.limit_per_source,
            "limit_per_source",
            minimum=1,
            maximum=self.limits.grep_max_limit_per_source,
        )
        regex = self._compile_pattern(
            pattern,
            mode=mode,
            case_sensitive=case_sensitive,
        )
        sources = request.sources
        resolved = self._resolve_content_sources(state, sources)
        outcomes = await self._fetch_content(
            state,
            resolved,
            query=None,
        )
        matches: list[dict[str, Any]] = []
        source_results: list[dict[str, Any]] = []
        failures: list[ContentFailure] = []
        for outcome in outcomes:
            if isinstance(outcome, ContentFailure):
                failures.append(outcome)
                continue
            input_index = outcome.input_index
            lines = self._document_lines(outcome)
            found = 0
            scan_complete = True
            next_start_line: int | None = None
            for index in range(start_line - 1, len(lines)):
                line = lines[index]
                spans = [
                    {
                        "start_character": matched.start(),
                        "end_character": matched.end(),
                    }
                    for matched in regex.finditer(line)
                ]
                if not spans:
                    continue
                found += 1
                before = lines[max(0, index - context_lines) : index] if context_lines else []
                after = lines[index + 1 : index + 1 + context_lines] if context_lines else []
                match = {
                    "source": outcome.source,
                    "title": outcome.title,
                    "line": index + 1,
                    "text": line,
                    "before": before,
                    "after": after,
                    "spans": spans,
                    "input_index": input_index,
                }
                matches.append(match)
                if found >= limit_per_source and index < len(lines) - 1:
                    scan_complete = False
                    next_start_line = index + 2
                    break
            source_results.append(
                {
                    "input_index": input_index,
                    "source": outcome.source,
                    "title": outcome.title,
                    "match_count": found,
                    "scan_complete": scan_complete,
                    "next_start_line": next_start_line,
                }
            )
        return ContentGrepReport(
            pattern=pattern,
            mode=mode,
            case_sensitive=case_sensitive,
            start_line=start_line,
            context_lines=context_lines,
            limit_per_source=limit_per_source,
            matches=matches,
            source_results=source_results,
            failures=failures,
            input_count=1 if isinstance(sources, str) else len(sources),
        ).model_dump(mode="json")

    def _document_fingerprint(
        self,
        binding: BackendBinding[DocumentBackend],
        handle: DocumentHandle,
    ) -> str:
        """Identify one logical document independently of provider fallbacks."""

        return self.providers.fingerprint(
            {
                "backend": binding.route,
                "revision": binding.revision,
                "identity": document_identity(binding.route, handle),
            }
        )

    @staticmethod
    def _fetch_candidates(
        binding: BackendBinding[DocumentBackend],
        handle: DocumentHandle,
    ) -> list[DocumentHandle]:
        try:
            candidates = _DOCUMENT_HANDLES.validate_python(
                binding.backend.fetch_candidates(handle),
                strict=True,
            )
        except ProviderRequestError:
            raise
        except Exception as exc:
            raise invalid_provider_response() from exc
        if not candidates or any(candidate.source != handle.source for candidate in candidates):
            raise invalid_provider_response()
        return candidates

    async def _fetch_document(
        self,
        state: BrokerSession,
        binding: BackendBinding[DocumentBackend],
        handle: DocumentHandle,
        candidate: DocumentHandle,
        *,
        query: str | None,
        request_index: int,
        request_id: str | None,
        track_execution: bool,
    ) -> DocumentContent:
        async def fetch(backend: DocumentBackend) -> DocumentContent:
            content = DocumentContent.model_validate(await backend.fetch(candidate, query=query))
            if content.source != handle.source:
                raise ValueError("backend changed the requested content source")
            return content

        validate_fetch = getattr(binding.backend, "preflight_fetch", None)

        def preflight() -> None:
            if callable(validate_fetch):
                validate_fetch(candidate)
            state.policy.record_content_backend_fetches(1)

        return await self.providers.execute(
            state,
            binding,
            request_indexes=[request_index],
            request_value={
                "backend": binding.route,
                "revision": binding.revision,
                "identity": document_identity(binding.route, handle),
                "representation": candidate.representation,
            },
            request=fetch,
            preflight=preflight,
            request_id=request_id,
            track_execution=track_execution,
        )

    async def _fetch_content(
        self,
        state: BrokerSession,
        sources: list[_SourceOutcome],
        *,
        query: str | None,
    ) -> list[_FetchOutcome]:
        """Fetch every admitted source and preserve failures as separate outcomes."""
        _route, document_binding = self._document_binding(state)

        def identity(item: _ResolvedSource) -> str:
            return document_identity(item.route, item.handle)

        resolved_sources = [item for item in sources if isinstance(item, _ResolvedSource)]
        keys_by_input: dict[int, str] = {}
        leaders: dict[str, tuple[int, _ResolvedSource]] = {}
        for item in resolved_sources:
            state.policy.require_backend(item.route)
            binding = self.document_bindings[item.route]
            fingerprint = self._document_fingerprint(binding, item.handle)
            keys_by_input[item.input_index] = fingerprint
            leader = leaders.get(fingerprint)
            if leader is None:
                leaders[fingerprint] = (item.input_index, item)
                continue
            self.providers.record_deduplicated_request(
                request_index=item.input_index,
                leader_index=leader[0],
                request_fingerprint=fingerprint,
            )

        duplicate_count = len(resolved_sources) - len(leaders)
        if duplicate_count:
            state.policy.record_deduplicated(duplicate_count)
        misses = {
            key: value
            for key, value in leaders.items()
            if identity(value[1]) not in state.content_cache
        }
        await state.policy.record_content_fetches(len(sources), 0)
        # The logical-usage reservation above may yield while another caller's
        # flight completes. Its transport leader writes the cache before
        # removing that flight, so refreshing here closes the only window in
        # which this call could admit a second leader for an already-cached
        # document.
        misses = {
            key: value
            for key, value in misses.items()
            if identity(value[1]) not in state.content_cache
        }

        async def fetch_one(
            key: str,
            input_index: int,
            item: _ResolvedSource,
            *,
            request_id: str | None = None,
            track_execution: bool = True,
        ) -> tuple[str, _FetchOutcome]:
            binding = self.document_bindings.get(item.route)
            if binding is None:
                return key, self._content_failure(
                    item,
                    {
                        "code": "provider_not_configured",
                        "message": f"Backend '{item.route}' is not configured.",
                        "retryable": False,
                        "attempts": 0,
                    },
                )
            try:
                candidates = self._fetch_candidates(binding, item.handle)
            except ProviderRequestError as exc:
                failure = self._contextualize_document_failure(
                    binding,
                    self.providers.provider_failure(exc),
                )
                return key, self._content_failure(item, failure)
            total_attempts = 0
            document: ContentDocument | None = None
            fallback_codes = {
                "provider_rejected",
                "provider_not_found",
                "provider_invalid_response",
            }
            for candidate_index, candidate in enumerate(candidates):
                candidate_request_id = (
                    f"{request_id}:{candidate_index}" if request_id is not None else None
                )
                try:
                    snippet = await self._fetch_document(
                        state,
                        binding,
                        item.handle,
                        candidate,
                        query=query,
                        request_index=input_index,
                        request_id=candidate_request_id,
                        track_execution=track_execution,
                    )
                except ProviderRequestError as exc:
                    failure = self.providers.provider_failure(exc)
                    total_attempts += int(failure.get("attempts") or 0)
                    has_fallback = candidate_index + 1 < len(candidates)
                    if has_fallback and failure["code"] in fallback_codes:
                        continue
                    failure["attempts"] = total_attempts
                    return key, self._content_failure(item, failure)

                metadata = dict(snippet.metadata)
                for metadata_key in ("ref", "url", "docid", "source"):
                    metadata.pop(metadata_key, None)
                if candidate.representation != "original":
                    metadata["representation"] = candidate.representation
                document = ContentDocument(
                    source=item.handle.source,
                    text=snippet.text,
                    title=snippet.title,
                    date=snippet.date or item.handle.date,
                    metadata=metadata,
                )
                break

            if document is None:
                return key, self._content_failure(
                    item,
                    {
                        "code": "provider_invalid_response",
                        "message": "Provider returned no document representation.",
                        "retryable": False,
                        "attempts": total_attempts,
                    },
                )
            document_id = identity(item)
            state.mark_fetched(document_id)
            return key, document

        async def collect_bounded(
            tasks: dict[str, asyncio.Task[tuple[str, _FetchOutcome]]],
        ) -> dict[str, _FetchOutcome]:
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
            results: dict[str, _FetchOutcome] = {}
            for fingerprint, task in tasks.items():
                if task not in done:
                    input_index, item = misses[fingerprint]
                    attempts = sum(
                        1
                        for record in current_provider_attempts()
                        if input_index in record.request_indexes
                    )
                    results[fingerprint] = self._content_failure(
                        item,
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
                    document_binding.namespace,
                    fingerprint,
                )
                for fingerprint in misses
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
                input_index, item = misses[fingerprint]
                document_id = identity(item)
                cached = state.content_cache.get(document_id)
                if cached is not None:

                    async def execute_cached_content(
                        flight_key: str = flight_key,
                        cached: dict[str, Any] = cached,
                    ) -> dict[str, _FetchOutcome]:
                        # Cache and flight admission are separate structures.
                        # A previous leader may populate the cache while this
                        # call waits for the flight lock; publish that row
                        # through the newly admitted future without starting a
                        # second provider request.
                        return {flight_key: ContentDocument.model_validate(copy.deepcopy(cached))}

                    self.providers.flights.start(state, group, execute_cached_content)
                    continue

                async def execute_content(
                    group: FlightGroup = group,
                    flight_key: str = flight_key,
                    fingerprint: str = fingerprint,
                    input_index: int = input_index,
                    item: _ResolvedSource = item,
                ) -> dict[str, _FetchOutcome]:
                    _key, outcome = await fetch_one(
                        fingerprint,
                        input_index,
                        item,
                        request_id=group.request_id,
                        track_execution=False,
                    )
                    # Publish successful content to the session before the
                    # flight runner removes its active key or resolves waiter
                    # futures. A caller queued behind flight cleanup therefore
                    # observes either the active flight or the cache, never a
                    # gap between the two.
                    if isinstance(outcome, ContentDocument):
                        state.cache_content(
                            identity(item),
                            outcome.model_dump(mode="json"),
                            self.session_content_cache_bytes,
                        )
                    return {flight_key: outcome}

                self.providers.flights.start(state, group, execute_content)

            async def await_content_flight(
                fingerprint: str,
            ) -> tuple[str, _FetchOutcome]:
                outcome = await self.providers.flights.wait(
                    state,
                    admission.waiters[flight_key_for_fingerprint[fingerprint]],
                )
                if not isinstance(outcome, (ContentDocument, ContentFailure)):
                    raise RuntimeError("Content flight returned an invalid outcome.")
                return fingerprint, outcome

            fetched = await collect_bounded(
                {
                    fingerprint: asyncio.create_task(await_content_flight(fingerprint))
                    for fingerprint in misses
                }
            )
        else:
            fetched = await collect_bounded(
                {
                    key: asyncio.create_task(fetch_one(key, input_index, item))
                    for key, (input_index, item) in misses.items()
                }
            )
        for fingerprint, outcome in fetched.items():
            _input_index, item = misses[fingerprint]
            if isinstance(outcome, ContentDocument):
                state.cache_content(
                    identity(item),
                    outcome.model_dump(mode="json"),
                    self.session_content_cache_bytes,
                )

        outcomes: list[_FetchOutcome] = []
        for source in sources:
            if isinstance(source, ContentFailure):
                outcomes.append(source)
                continue
            fingerprint = keys_by_input[source.input_index]
            cached = state.content_cache.get(identity(source))
            outcome: _FetchOutcome | None = (
                ContentDocument.model_validate(copy.deepcopy(cached))
                if cached is not None
                else fetched.get(fingerprint)
            )
            if outcome is None:
                outcome = self._content_failure(
                    source,
                    {
                        "code": "provider_invalid_response",
                        "message": "Provider returned no result for this document.",
                        "retryable": False,
                        "attempts": 0,
                    },
                )
            if isinstance(outcome, ContentFailure):
                outcomes.append(
                    outcome.model_copy(
                        update={
                            "input_index": source.input_index,
                            "source": source.handle.source,
                        }
                    )
                )
                continue
            if source.register_on_success:
                candidate_source = normalize_web_source(source.handle.source)
                aliases = {candidate_source}
                if source.handle.url:
                    aliases.add(normalize_source(source.handle.url))
                state.remember(
                    source.route,
                    source.handle.model_copy(update={"source": candidate_source}),
                    identity=identity(source),
                    admission="direct_url",
                    aliases=aliases,
                )
                state.policy.record_direct_url_success()
            state.mark_fetched(identity(source))
            outcomes.append(
                ContentResult(
                    input_index=source.input_index,
                    source=source.handle.source,
                    text=outcome.text,
                    title=outcome.title or source.handle.title,
                    date=outcome.date or source.handle.date,
                    metadata=outcome.metadata,
                )
            )

        return outcomes

    def _content_failure(
        self,
        item: _ResolvedSource,
        failure: dict[str, Any],
    ) -> ContentFailure:
        binding = self.document_bindings.get(item.route)
        if binding is not None:
            failure = self._contextualize_document_failure(binding, failure)
        return ContentFailure(
            input_index=item.input_index,
            source=item.handle.source,
            **failure,
        )
