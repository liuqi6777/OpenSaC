from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ._diagnostics import (
    error_info,
    failure_detail,
    record_external_failures,
)
from ._json import atomic_write_text, strict_json_dumps, strict_jsonl_dumps
from ._many import _ManyFailure, _ManySuccess, _run_many
from ._optional import capture_optional
from ._record import Record, record, wrap
from ._validation import (
    boolean,
    finite_number,
    integer,
    optional_integer,
    optional_string,
    optional_string_list,
    string,
    string_list,
)
from .transport import BrokerError, UnixSocketTransport


class SearchResource:
    """Find documents and combine ranked search result sets.

    Call ``sdk.search(query, limit=10, offset=0, include_domains=None)`` for one ranked
    window. Each hit has one public ``source``: a canonical web URL for web
    results or a document ID for local results. An empty list is a successful
    no-match result.

    Pass returned ``source`` values to ``sdk.content``. Web deployments may also
    accept bounded public HTTP(S) URLs directly, according to host policy.
    """

    def __init__(self, transport: UnixSocketTransport) -> None:
        self._transport = transport

    def __call__(
        self,
        query: str,
        *,
        limit: int = 10,
        offset: int = 0,
        include_domains: list[str] | None = None,
    ) -> list[Record] | None:
        """Search one query and return a ranked hit window when available.

        ``offset`` is depth in the full ranking, not a page number.
        ``include_domains`` is accepted only by backends that support filtering.

        Successful hits are ordered by ``rank`` and include ``source``, ``backend``,
        ``title``, ``snippet``, ``score``, ``rank``, ``retrieval``, and ``metadata``.
        An empty list is a successful no-match search. Operational failures return
        ``None`` and are recorded as structured warnings.
        """
        query = string(query, "query", strip=True)
        limit, offset = self._search_window(limit, offset)
        include_domains = optional_string_list(include_domains, "include_domains")
        return capture_optional(
            "search",
            lambda: self._query(
                query,
                limit=limit,
                offset=offset,
                include_domains=include_domains,
            ),
            query=query,
        )

    def _query(
        self,
        query: str,
        *,
        limit: int,
        offset: int,
        include_domains: list[str] | None,
    ) -> list[Record]:
        return self._transport.call(
            "search.query",
            {
                "query": query,
                "limit": limit,
                "offset": offset,
                "include_domains": include_domains,
            },
        )

    def many(
        self,
        queries: list[str],
        *,
        limit: int = 10,
        offset: int = 0,
        concurrency: int = 5,
        include_domains: list[str] | None = None,
    ) -> list[list[Record] | None]:
        """Search several queries with bounded concurrency and aligned results.

        ``limit`` and ``offset`` define each ranked window. ``concurrency`` bounds
        helper fan-out; the broker remains authoritative for actual provider
        concurrency. ``include_domains`` has the same semantics as single-query
        search.

        Returns one ranked hit list or ``None`` per input query, in input order.
        Operational failures do not raise exceptions and are recorded as structured
        warnings. An empty hit list is a successful no-match search.
        """
        queries = string_list(queries, "queries")
        limit, offset = self._search_window(limit, offset)
        concurrency = integer(concurrency, "concurrency", minimum=1)
        include_domains = optional_string_list(include_domains, "include_domains")
        return self._many_concurrent(
            queries,
            limit=limit,
            offset=offset,
            concurrency=concurrency,
            include_domains=include_domains,
        )

    def _many_concurrent(
        self,
        queries: list[str],
        *,
        limit: int,
        offset: int,
        concurrency: int,
        include_domains: list[str] | None,
    ) -> list[list[Record] | None]:
        if not queries:
            return []
        try:
            self._validate_many_admission(
                queries,
                limit=limit,
                offset=offset,
                concurrency=concurrency,
                include_domains=include_domains,
            )
        except BrokerError as error:
            info = error_info(error)
            record_external_failures(
                "search.many",
                success_count=0,
                failures=[
                    failure_detail(info, input_index=input_index, query=query)
                    for input_index, query in enumerate(queries)
                ],
            )
            return [None] * len(queries)

        def search_one(query: str) -> list[Record]:
            return self._query(
                query,
                limit=limit,
                offset=offset,
                include_domains=include_domains,
            )

        report = _run_many(queries, concurrency=concurrency, call=search_one)
        results: list[list[Record] | None] = []
        for result in report.outcomes:
            if isinstance(result, _ManySuccess):
                results.append(result.value)
                continue
            if not isinstance(result, _ManyFailure):
                raise RuntimeError("many search returned an invalid internal outcome")
            results.append(None)

        report.record_failures(
            "search.many",
            detail=lambda failure: failure_detail(
                failure.info,
                input_index=failure.input_index,
                query=failure.item,
            ),
        )
        return results

    def _validate_many_admission(
        self,
        queries: list[str],
        *,
        limit: int,
        offset: int,
        concurrency: int,
        include_domains: list[str] | None,
    ) -> None:
        manifest = self._transport.call("session.capabilities", {})
        try:
            search = manifest["search"]
            limits = search["limits"]
            mechanisms = manifest["mechanisms"]
            supports_domains = search["supports_include_domains"]
            max_depth = search["max_depth"]
            batching = mechanisms["batching"]
        except (KeyError, TypeError) as exc:
            raise self._protocol_error(
                "session.capabilities", "an invalid search manifest"
            ) from exc

        if not isinstance(supports_domains, bool) or not isinstance(batching, bool):
            raise self._protocol_error("session.capabilities", "an invalid search manifest")
        if max_depth is not None and (
            isinstance(max_depth, bool) or not isinstance(max_depth, int) or max_depth < 1
        ):
            raise self._protocol_error("session.capabilities", "an invalid search manifest")

        max_queries = self._manifest_limit(limits, "max_queries_per_request")
        max_concurrency = self._manifest_limit(limits, "max_concurrency")
        max_limit = self._manifest_limit(limits, "max_limit")
        max_offset = self._manifest_limit(limits, "max_offset", minimum=0)
        max_top_k = self._manifest_limit(limits, "max_top_k")

        if len(queries) > max_queries:
            raise self._admission_error(
                f"search.many contains {len(queries)} queries, exceeding the broker maximum "
                f"of {max_queries}"
            )
        if not batching and len(queries) > 1:
            raise self._admission_error(
                "Batching is disabled for this session: search.many accepts at most one query.",
                code="capability_disabled",
            )
        if concurrency > max_concurrency:
            raise self._admission_error(
                f"concurrency must be at most {max_concurrency} for search.many"
            )
        if limit > max_limit:
            raise self._admission_error(f"limit must be at most {max_limit}")
        if offset > max_offset:
            raise self._admission_error(f"offset must be at most {max_offset}")
        depth = offset + limit
        if depth > max_top_k:
            raise self._admission_error(
                f"offset={offset} with limit={limit} asks for retrieval depth {depth}, "
                f"exceeding the broker maximum of {max_top_k}"
            )
        if max_depth is not None and depth > max_depth:
            raise self._admission_error(
                f"The active backend reaches rank {max_depth} at most, and offset={offset} "
                f"with limit={limit} asks for {depth}."
            )
        if include_domains and not supports_domains:
            raise self._admission_error(
                "The active backend has no domain filter, so include_domains cannot be honoured."
            )

    @classmethod
    def _manifest_limit(
        cls,
        limits: Any,
        field: str,
        *,
        minimum: int = 1,
    ) -> int:
        try:
            value = limits[field]
        except (KeyError, TypeError) as exc:
            raise cls._protocol_error(
                "session.capabilities", "an invalid search limits manifest"
            ) from exc
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise cls._protocol_error("session.capabilities", "an invalid search limits manifest")
        return value

    @staticmethod
    def _admission_error(message: str, *, code: str = "invalid_request") -> BrokerError:
        return BrokerError(message, code=code, retryable=False, attempts=0)

    @staticmethod
    def _search_window(limit: Any, offset: Any) -> tuple[int, int]:
        return integer(limit, "limit", minimum=1), integer(offset, "offset", minimum=0)

    @staticmethod
    def _validate_report_size(method: str, reported: Any, expected: int) -> None:
        if isinstance(reported, bool) or not isinstance(reported, int) or reported != expected:
            raise SearchResource._protocol_error(method, "an invalid input_count")

    @staticmethod
    def _claim_result(method: str, assigned: list[bool], input_index: Any) -> int:
        if (
            isinstance(input_index, bool)
            or not isinstance(input_index, int)
            or input_index < 0
            or input_index >= len(assigned)
            or assigned[input_index]
        ):
            raise SearchResource._protocol_error(method, "invalid result alignment")
        assigned[input_index] = True
        return input_index

    @staticmethod
    def _require_complete_report(method: str, assigned: list[bool]) -> None:
        if not all(assigned):
            raise SearchResource._protocol_error(method, "incomplete result alignment")

    @staticmethod
    def _protocol_error(method: str, detail: str) -> BrokerError:
        return BrokerError(
            f"{method} returned {detail}",
            code="broker_protocol_error",
            retryable=False,
        )

    def fuse_rrf(
        self,
        queries: list[str],
        results: list[list[Record] | None],
        *,
        weights: list[float] | None = None,
        k: int = 60,
        limit: int | None = None,
        exclude_domains: list[str] | None = None,
        domain_weights: dict[str, float] | None = None,
        max_per_domain: int | None = None,
    ) -> list[Record]:
        """Fuse aligned multi-query search results locally with domain-aware RRF.

        This deterministic helper makes no broker call. ``queries`` and ``results``
        align by input position. ``weights`` includes failed ``None`` positions, and
        provenance derives ``query`` and ``input_index`` from that alignment.
        ``k`` controls rank smoothing and ``limit`` truncates
        the fused list. Domain policies match an exact hostname or any subdomain.
        ``exclude_domains`` removes candidates, ``domain_weights`` multiplies their
        RRF scores, and ``max_per_domain`` caps candidates sharing one exact hostname
        before the final limit is applied. Sources that are not web URLs are
        unaffected.

        Returns:
            Search-hit records extended with ``provenance``, ``raw_fused_score``,
            ``domain_weight``, ``fused_score``, and 1-based ``fused_rank``. Document
            sources and metadata are preserved.

        Raises:
            ValueError: Weights, domains, ranks, ``k``, or limits are invalid.
        """
        normalized_queries = string_list(queries, "queries")
        if not isinstance(results, list):
            raise ValueError("results must be the list returned by search.many")
        if len(normalized_queries) != len(results):
            raise ValueError("queries and results must have the same length")
        parsed_batches: list[list[Record] | None] = []
        for batch in results:
            if batch is None:
                parsed_batches.append(None)
                continue
            if not isinstance(batch, list):
                raise ValueError("Every search result batch must be a list or None")
            parsed_batch: list[Record] = []
            for hit in batch:
                if not isinstance(hit, dict):
                    raise ValueError("Every fused search hit must be a mapping")
                parsed_batch.append(record(hit))
            parsed_batches.append(parsed_batch)
        normalized_weights = self._validate_fusion_options(
            len(parsed_batches), weights=weights, k=k, limit=limit
        )
        normalized_exclusions = self._normalize_domain_list(
            exclude_domains, option="exclude_domains"
        )
        normalized_domain_weights = self._normalize_domain_weights(domain_weights)
        self._validate_max_per_domain(max_per_domain)
        candidates: dict[str, dict[str, Any]] = {}

        for input_index, batch in enumerate(parsed_batches):
            if batch is None:
                continue
            weight = normalized_weights[input_index]
            best_in_batch: dict[str, tuple[int, Record]] = {}
            for hit_index, hit in enumerate(batch):
                if hit.rank < 1:
                    raise ValueError("Every fused search hit must have rank >= 1")
                previous = best_in_batch.get(hit.source)
                if previous is None or (hit.rank, hit_index) < (
                    previous[1].rank,
                    previous[0],
                ):
                    best_in_batch[hit.source] = (hit_index, hit)

            for hit_index, hit in best_in_batch.values():
                provenance = record(
                    {
                        "input_index": input_index,
                        "query": normalized_queries[input_index],
                        "backend": hit.backend,
                        "rank": hit.rank,
                        "score": hit.get("score"),
                    }
                )
                representative_key = (hit.rank, input_index, hit_index)
                candidate = candidates.get(hit.source)
                if candidate is None:
                    candidates[hit.source] = {
                        "hit": hit,
                        "representative_key": representative_key,
                        "best_rank": hit.rank,
                        "earliest_input": input_index,
                        "provenance": [provenance],
                        "fused_score": weight / (k + hit.rank),
                        "domain": self._source_domain(hit.source),
                    }
                    continue

                candidate["provenance"].append(provenance)
                candidate["fused_score"] += weight / (k + hit.rank)
                candidate["best_rank"] = min(candidate["best_rank"], hit.rank)
                candidate["earliest_input"] = min(candidate["earliest_input"], input_index)
                if representative_key < candidate["representative_key"]:
                    candidate["hit"] = hit
                    candidate["representative_key"] = representative_key

        eligible: list[tuple[str, dict[str, Any]]] = []
        for source, candidate in candidates.items():
            domain = candidate["domain"]
            if domain is not None and self._domain_matches_any(domain, normalized_exclusions):
                continue
            raw_fused_score = candidate["fused_score"]
            domain_weight = self._domain_weight(domain, normalized_domain_weights)
            candidate["raw_fused_score"] = raw_fused_score
            candidate["domain_weight"] = domain_weight
            candidate["fused_score"] = raw_fused_score * domain_weight
            eligible.append((source, candidate))

        ordered = sorted(
            eligible,
            key=lambda item: (
                -item[1]["fused_score"],
                item[1]["best_rank"],
                item[1]["earliest_input"],
                item[0],
            ),
        )
        selected: list[tuple[str, dict[str, Any]]] = []
        domain_counts: dict[str, int] = {}
        for item in ordered:
            if limit is not None and len(selected) >= limit:
                break
            domain = item[1]["domain"]
            if domain is not None and max_per_domain is not None:
                if domain_counts.get(domain, 0) >= max_per_domain:
                    continue
                domain_counts[domain] = domain_counts.get(domain, 0) + 1
            selected.append(item)

        fused = [
            record(
                {
                    **candidate["hit"],
                    "provenance": candidate["provenance"],
                    "raw_fused_score": candidate["raw_fused_score"],
                    "domain_weight": candidate["domain_weight"],
                    "fused_score": candidate["fused_score"],
                    "fused_rank": fused_rank,
                }
            )
            for fused_rank, (_, candidate) in enumerate(selected, start=1)
        ]
        return fused

    @staticmethod
    def _validate_fusion_options(
        batch_count: int,
        *,
        weights: list[float] | None,
        k: int,
        limit: int | None,
    ) -> list[float]:
        if isinstance(k, bool) or not isinstance(k, int) or k < 0:
            raise ValueError("k must be a non-negative integer")
        if limit is not None and (
            isinstance(limit, bool) or not isinstance(limit, int) or limit < 0
        ):
            raise ValueError("limit must be a non-negative integer or None")

        if weights is None:
            normalized = [1.0] * batch_count
        else:
            if len(weights) != batch_count:
                raise ValueError("weights must align one-to-one with batches")
            normalized = []
            for weight in weights:
                if isinstance(weight, bool) or not isinstance(weight, (int, float)):
                    raise ValueError("weights must contain only finite numbers")
                normalized_weight = float(weight)
                if not math.isfinite(normalized_weight):
                    raise ValueError("weights must contain only finite numbers")
                if normalized_weight < 0:
                    raise ValueError("weights must be non-negative")
                normalized.append(normalized_weight)

        if normalized and not any(weight > 0 for weight in normalized):
            raise ValueError("at least one weight must be greater than zero")
        return normalized

    @classmethod
    def _normalize_domain_list(
        cls,
        domains: list[str] | None,
        *,
        option: str,
    ) -> frozenset[str]:
        if domains is None:
            return frozenset()
        if not isinstance(domains, list):
            raise ValueError(f"{option} must be a list of domain names or None")
        return frozenset(cls._normalize_policy_domain(domain, option=option) for domain in domains)

    @classmethod
    def _normalize_domain_weights(
        cls,
        domain_weights: dict[str, float] | None,
    ) -> dict[str, float]:
        if domain_weights is None:
            return {}
        if not isinstance(domain_weights, dict):
            raise ValueError("domain_weights must be a mapping of domains to weights or None")
        normalized: dict[str, float] = {}
        for domain, weight in domain_weights.items():
            normalized_domain = cls._normalize_policy_domain(domain, option="domain_weights")
            if normalized_domain in normalized:
                raise ValueError("domain_weights contains duplicate normalized domains")
            if isinstance(weight, bool) or not isinstance(weight, (int, float)):
                raise ValueError("domain_weights must contain only positive finite numbers")
            normalized_weight = float(weight)
            if not math.isfinite(normalized_weight) or normalized_weight <= 0:
                raise ValueError("domain_weights must contain only positive finite numbers")
            normalized[normalized_domain] = normalized_weight
        return normalized

    @staticmethod
    def _normalize_policy_domain(domain: Any, *, option: str) -> str:
        if not isinstance(domain, str):
            raise ValueError(f"{option} must contain only domain names")
        value = domain.strip().rstrip(".").lower()
        if not value or any(character in value for character in "/:@?#"):
            raise ValueError(f"{option} must contain only domain names")
        try:
            value = value.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise ValueError(f"{option} contains an invalid domain name") from exc
        labels = value.split(".")
        if len(value) > 253 or any(
            not label
            or len(label) > 63
            or label.startswith("-")
            or label.endswith("-")
            or not all(character.isalnum() or character == "-" for character in label)
            for label in labels
        ):
            raise ValueError(f"{option} contains an invalid domain name")
        return value

    @staticmethod
    def _source_domain(source: Any) -> str | None:
        if not isinstance(source, str):
            return None
        try:
            parts = urlsplit(source)
            hostname = parts.hostname
        except ValueError:
            return None
        if parts.scheme.lower() not in {"http", "https"} or not hostname:
            return None
        try:
            return hostname.rstrip(".").encode("idna").decode("ascii").lower()
        except UnicodeError:
            return hostname.rstrip(".").lower()

    @staticmethod
    def _domain_matches(domain: str, policy_domain: str) -> bool:
        return domain == policy_domain or domain.endswith(f".{policy_domain}")

    @classmethod
    def _domain_matches_any(cls, domain: str, policy_domains: frozenset[str]) -> bool:
        return any(cls._domain_matches(domain, policy_domain) for policy_domain in policy_domains)

    @classmethod
    def _domain_weight(cls, domain: str | None, weights: dict[str, float]) -> float:
        if domain is None:
            return 1.0
        matches = [
            (len(policy_domain), weight)
            for policy_domain, weight in weights.items()
            if cls._domain_matches(domain, policy_domain)
        ]
        return max(matches, default=(0, 1.0))[1]

    @staticmethod
    def _validate_max_per_domain(max_per_domain: int | None) -> None:
        if max_per_domain is not None and (
            isinstance(max_per_domain, bool)
            or not isinstance(max_per_domain, int)
            or max_per_domain < 1
        ):
            raise ValueError("max_per_domain must be a positive integer or None")


class ContentResource:
    """Locate and read text from URL or local-document source strings.

    Fetch a small relevant source set before inspecting it, then reuse the text locally.
    ``passages`` adds semantic ranking across selected documents; ``grep`` and ``read`` provide
    optional service-side matches and windows. These methods may reuse the session cache but remain
    separate logical requests. Broker-backed methods return generic outcomes; collection tasks
    retain per-source failures in aligned outcome lists or successful reports.
    """

    def __init__(self, transport: UnixSocketTransport) -> None:
        self._transport = transport

    @staticmethod
    def _sources(sources: list[str]) -> list[str]:
        if not isinstance(sources, list):
            raise ValueError("sources must be a list of source strings")
        validated: list[str] = []
        for input_index, source in enumerate(sources):
            if not isinstance(source, str):
                raise ValueError(f"source at input index {input_index} must be a string")
            source = source.strip()
            if not source:
                raise ValueError(f"source at input index {input_index} must not be empty")
            validated.append(source)
        return validated

    @classmethod
    def _source(cls, source: str) -> str:
        try:
            return cls._sources([source])[0]
        except ValueError as exc:
            message = str(exc).replace("source at input index 0", "source")
            raise ValueError(message) from None

    def fetch(self, source: str) -> Record | None:
        """Fetch one complete normalized document when available.

        A successful document contains ``source``, ``title``, ``date``, ``text``, and
        provider-owned ``metadata``. Reuse it locally for several checks, optionally
        persist one copy with ``sdk.workspace``, and never print the complete text.
        Operational failures return ``None`` and are recorded as structured warnings.
        """
        source = self._source(source)
        return capture_optional(
            "content.fetch",
            lambda: self._fetch(source),
            source=source,
        )

    def _fetch(self, source: str) -> Record:
        return self._transport.call("content.fetch", {"source": source})

    def fetch_many(
        self,
        sources: list[str],
        *,
        concurrency: int = 5,
    ) -> list[Record | None]:
        """Fetch several complete documents with bounded concurrency and aligned results.

        ``concurrency`` bounds SDK helper fan-out; each item remains an independent
        ``content.fetch`` request governed by broker budget, retry, cache, tracing, and
        provider-concurrency policies. Input order and duplicate sources are preserved.

        Returns one document or ``None`` per input source. Operational failures never
        escape as ``BrokerError`` and are recorded as structured warnings.
        """
        sources = self._sources(sources)
        concurrency = integer(concurrency, "concurrency", minimum=1)
        report = _run_many(sources, concurrency=concurrency, call=self._fetch)

        results: list[Record | None] = []
        for result in report.outcomes:
            if isinstance(result, _ManySuccess):
                results.append(result.value)
                continue
            if not isinstance(result, _ManyFailure):
                raise RuntimeError("many fetch returned an invalid internal outcome")
            results.append(None)

        report.record_failures(
            "content.fetch_many",
            detail=lambda failure: failure_detail(
                failure.info,
                input_index=failure.input_index,
                source=failure.item,
            ),
        )
        return results

    def read(
        self,
        source: str,
        *,
        start_line: int = 1,
        start_character: int = 0,
        line_count: int = 200,
        max_chars: int = 100_000,
    ) -> Record | None:
        """Read one 1-indexed line window from a source when available.

        Lines are 1-based and characters are 0-based. ``line_count`` bounds logical
        lines and ``max_chars`` bounds returned text. Pass ``window.next`` back as
        ``start_line`` and ``start_character`` to continue without losing text. In a
        fetch-first workflow, local slicing usually avoids this additional logical request.

        A successful result contains one content slice with provider ``metadata`` and
        a separate ``window``. Operational failures return ``None`` and are recorded as
        structured warnings.
        """
        start_line = integer(start_line, "start_line", minimum=1)
        start_character = integer(start_character, "start_character", minimum=0)
        line_count = integer(line_count, "line_count", minimum=1)
        max_chars = integer(max_chars, "max_chars", minimum=1)
        source = self._source(source)
        return capture_optional(
            "content.read",
            lambda: self._transport.call(
                "content.read",
                {
                    "source": source,
                    "start_line": start_line,
                    "start_character": start_character,
                    "line_count": line_count,
                    "max_chars": max_chars,
                },
            ),
            source=source,
        )

    def grep(
        self,
        pattern: str,
        *,
        sources: list[str],
        mode: str = "regex",
        case_sensitive: bool = False,
        start_line: int = 1,
        context_lines: int = 0,
        limit_per_source: int = 20,
    ) -> list[Record | None]:
        """Search document lines and preserve per-source failures.

        ``mode`` selects regular-expression or literal matching. ``start_line`` is
        1-based, ``context_lines`` adds surrounding lines, and ``limit_per_source``
        bounds each document's contribution. In a fetch-first workflow, local matching
        usually avoids this additional logical request.

        Returns one result record or ``None`` per source, in input order. Successful
        records own their ``matches`` and continuation cursor. Operational failures are
        recorded as structured warnings.
        """
        pattern = string(pattern, "pattern")
        if mode not in {"regex", "literal"}:
            raise ValueError("mode must be 'regex' or 'literal'")
        case_sensitive = boolean(case_sensitive, "case_sensitive")
        start_line = integer(start_line, "start_line", minimum=1)
        context_lines = integer(context_lines, "context_lines", minimum=0)
        limit_per_source = integer(limit_per_source, "limit_per_source", minimum=1)
        sources = self._sources(sources)
        if not sources:
            return []
        try:
            return self._grep(
                pattern,
                sources=sources,
                mode=mode,
                case_sensitive=case_sensitive,
                start_line=start_line,
                context_lines=context_lines,
                limit_per_source=limit_per_source,
            )
        except BrokerError as error:
            info = error_info(error)
            record_external_failures(
                "content.grep",
                success_count=0,
                failures=[
                    failure_detail(info, input_index=input_index, source=source)
                    for input_index, source in enumerate(sources)
                ],
            )
            return [None] * len(sources)

    def _grep(
        self,
        pattern: str,
        *,
        sources: list[str],
        mode: str,
        case_sensitive: bool,
        start_line: int,
        context_lines: int,
        limit_per_source: int,
    ) -> list[Record]:
        report = self._transport.call(
            "content.grep",
            {
                "sources": sources,
                "pattern": pattern,
                "mode": mode,
                "case_sensitive": case_sensitive,
                "start_line": start_line,
                "context_lines": context_lines,
                "limit_per_source": limit_per_source,
            },
        )
        failures = [
            failure_detail(row, input_index=row.input_index, source=row.source)
            for row in report.failures
        ]
        record_external_failures(
            "content.grep",
            success_count=report.input_count - len(failures),
            failures=failures,
        )
        SearchResource._validate_report_size("content.grep", report.input_count, len(sources))
        matches_by_input: list[list[Record]] = [[] for _ in sources]
        for match in report.matches:
            input_index = match.input_index
            if (
                isinstance(input_index, bool)
                or not isinstance(input_index, int)
                or input_index < 0
                or input_index >= len(sources)
            ):
                raise SearchResource._protocol_error("content.grep", "invalid match alignment")
            matches_by_input[input_index].append(
                record(
                    {
                        "line": match.line,
                        "text": match.text,
                        "before": match.get("before", []),
                        "after": match.get("after", []),
                        "spans": match.spans,
                    }
                )
            )

        results: list[Record | None] = [None] * len(sources)
        assigned = [False] * len(sources)
        for result in report.source_results:
            input_index = SearchResource._claim_result("content.grep", assigned, result.input_index)
            results[input_index] = record(
                {
                    "source": result.source,
                    "title": result.title,
                    "matches": matches_by_input[input_index],
                    "next_start_line": result.next_start_line,
                }
            )
        for failed in report.failures:
            input_index = SearchResource._claim_result("content.grep", assigned, failed.input_index)
            if matches_by_input[input_index]:
                raise SearchResource._protocol_error("content.grep", "matches for a failed source")
        SearchResource._require_complete_report("content.grep", assigned)
        return results

    def passages(
        self,
        query: str,
        *,
        sources: list[str],
        limit: int = 20,
        limit_per_source: int = 3,
    ) -> Record | None:
        """Rank passages across a caller-supplied source set when available.

        The broker deduplicates sources in first-seen order, ranks successful documents
        together, then applies ``limit_per_source``. ``limit`` bounds the report.
        Scores are comparable only within this report. This semantic ranking can add value
        after fetch when ordinary local lexical checks are insufficient.

        A successful result is a report with ``query``, ``passages``, fetch
        ``failures``, reranker ``warnings``, ``input_count``, and ``unique_source_count``.
        Operational whole-report failures return ``None`` and are recorded as warnings.
        """
        query = string(query, "query", strip=True)
        limit = integer(limit, "limit", minimum=1)
        limit_per_source = integer(limit_per_source, "limit_per_source", minimum=1)
        sources = self._sources(sources)
        return capture_optional(
            "content.passages",
            lambda: self._passages(
                query,
                sources=sources,
                limit=limit,
                limit_per_source=limit_per_source,
            ),
            query=query,
        )

    def _passages(
        self,
        query: str,
        *,
        sources: list[str],
        limit: int,
        limit_per_source: int,
    ) -> Record:
        report = self._transport.call(
            "content.passages",
            {
                "query": query,
                "sources": sources,
                "limit": limit,
                "limit_per_source": limit_per_source,
            },
        )
        failures = [
            failure_detail(row, input_index=row.input_index, source=row.source)
            for row in report.failures
        ]
        record_external_failures(
            "content.passages",
            success_count=max(report.unique_source_count - len(failures), 0),
            failures=failures,
        )
        rerank_warnings = [failure_detail(warning) for warning in report.get("warnings", [])]
        record_external_failures(
            "content.passages.rerank",
            success_count=0,
            failures=rerank_warnings,
        )
        return report


class LLMResource:
    """Use the optional pipeline model for bounded semantic subroutines.

    Prefer deterministic Python whenever it is sufficient. Use ``extract`` for
    structured results; free-form completion methods are advanced operations.
    """

    def __init__(self, transport: UnixSocketTransport) -> None:
        self._transport = transport

    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        temperature: float = 0.2,
        max_tokens: int | None = None,
    ) -> str | None:
        """Run one free-form pipeline-model completion when available.

        ``system`` supplies optional instructions, ``temperature`` controls sampling,
        and ``max_tokens`` optionally bounds the response. Prefer ``extract``
        when downstream code expects structured data.

        A successful result is the model's response text. Operational failures,
        including unavailable pipeline-model capability, return ``None`` and are
        recorded as structured warnings.
        """
        prompt = string(prompt, "prompt")
        system = optional_string(system, "system")
        temperature = finite_number(
            temperature,
            "temperature",
            minimum=0.0,
            maximum=2.0,
        )
        max_tokens = optional_integer(
            max_tokens,
            "max_tokens",
            minimum=1,
        )
        return capture_optional(
            "llm.complete",
            lambda: self._transport.call(
                "llm.complete",
                {
                    "prompt": prompt,
                    "system": system,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                },
            ),
        )

    def extract(
        self,
        item: Any,
        *,
        instruction: str,
        schema: dict[str, Any],
        max_tokens: int | None = None,
        repair_attempts: int = 0,
    ) -> Record | None:
        """Transform one JSON value into a schema-checked result when available.

        ``item`` and ``schema`` must be strict-JSON serializable. The schema root
        must be an object. ``repair_attempts`` must be non-negative and cannot
        exceed the broker-advertised limit.

        A successful result is the validated JSON object. Provider, JSON, schema,
        repair, and quota failures return ``None`` and are recorded as structured
        warnings. Invalid local arguments still raise ``ValueError``.
        """
        instruction = string(instruction, "instruction", nonempty=False)
        max_tokens = optional_integer(
            max_tokens,
            "max_tokens",
            minimum=1,
        )
        repair_attempts = integer(
            repair_attempts,
            "repair_attempts",
            minimum=0,
        )
        if not isinstance(schema, dict):
            raise ValueError("schema must be a JSON-serializable object")
        self._ensure_json_serializable(schema, "schema")
        self._ensure_json_serializable(item, "item")

        return capture_optional(
            "llm.extract",
            lambda: self._extract(
                item,
                instruction=instruction,
                schema=schema,
                max_tokens=max_tokens,
                repair_attempts=repair_attempts,
            ),
        )

    def _extract(
        self,
        item: Any,
        *,
        instruction: str,
        schema: dict[str, Any],
        max_tokens: int | None,
        repair_attempts: int,
    ) -> Record:
        params = {
            "item": item,
            "instruction": instruction,
            "schema": schema,
            "repair_attempts": repair_attempts,
        }
        if max_tokens is not None:
            params["max_tokens"] = max_tokens
        return self._transport.call("llm.extract", params)

    def extract_many(
        self,
        items: list[Any],
        *,
        instruction: str,
        schema: dict[str, Any],
        concurrency: int = 4,
        max_tokens: int | None = None,
        repair_attempts: int = 0,
    ) -> list[Record | None]:
        """Extract several JSON items with bounded concurrency and aligned results.

        Every item uses the same instruction, schema, token bound, and repair policy.
        ``concurrency`` bounds SDK helper fan-out; each item remains an independent
        ``llm.extract`` request governed by broker budget, retry, and provider policies.
        Original items are never copied into failure diagnostics.

        Returns one schema-validated object or ``None`` per input position. Operational
        failures are recorded as structured warnings. Invalid local arguments still
        raise ``ValueError``.
        """
        if not isinstance(items, list):
            raise ValueError("items must be a list of strict-JSON values")
        instruction = string(instruction, "instruction", nonempty=False)
        concurrency = integer(concurrency, "concurrency", minimum=1)
        max_tokens = optional_integer(max_tokens, "max_tokens", minimum=1)
        repair_attempts = integer(repair_attempts, "repair_attempts", minimum=0)
        if not isinstance(schema, dict):
            raise ValueError("schema must be a JSON-serializable object")
        self._ensure_json_serializable(schema, "schema")
        for input_index, item in enumerate(items):
            self._ensure_json_serializable(item, f"items[{input_index}]")

        def extract_one(item: Any) -> Record:
            return self._extract(
                item,
                instruction=instruction,
                schema=schema,
                max_tokens=max_tokens,
                repair_attempts=repair_attempts,
            )

        report = _run_many(items, concurrency=concurrency, call=extract_one)
        results: list[Record | None] = []
        for result in report.outcomes:
            if isinstance(result, _ManySuccess):
                results.append(result.value)
                continue
            if not isinstance(result, _ManyFailure):
                raise RuntimeError("many extraction returned an invalid internal outcome")
            results.append(None)

        report.record_failures(
            "llm.extract_many",
            detail=lambda failure: failure_detail(
                failure.info,
                input_index=failure.input_index,
            ),
        )
        return results

    @staticmethod
    def _ensure_json_serializable(value: Any, field: str) -> None:
        strict_json_dumps(value, field=field)


class CapabilitiesResource:
    """Inspect session-visible deployment capabilities and contract versions."""

    def __init__(self, transport: UnixSocketTransport) -> None:
        self._transport = transport

    def __call__(self) -> Record | None:
        """Return session-visible capabilities, limits, and contract versions when available.

        The record reflects the active backend and this session's mechanism
        switches without exposing provider credentials or internal endpoints.
        """
        return capture_optional(
            "capabilities",
            lambda: self._transport.call("session.capabilities", {}),
        )


class WorkspaceResource:
    """Persist structured artifacts across executions in one live session.

    Paths are workspace-relative and cannot escape the session workspace. The
    workspace is program memory, not a database; local document sources become
    invalid if the host reports ``state_lost``. Public web URLs remain meaningful
    across sessions.
    """

    def __init__(self, workspace: str | None) -> None:
        self._workspace = Path(workspace).resolve() if workspace is not None else None

    def _workspace_path(self) -> Path:
        if self._workspace is not None:
            return self._workspace
        return Path(os.environ.get("OPENSAC_WORKSPACE", "/workspace")).resolve()

    def _path(self, relative_path: str) -> Path:
        workspace = self._workspace_path()
        path = (workspace / relative_path).resolve()
        if not path.is_relative_to(workspace):
            raise ValueError("Workspace path must remain inside the session workspace")
        return path

    @staticmethod
    def _dump(rows: list[Any]) -> str:
        return strict_jsonl_dumps(rows)

    def write_jsonl(self, relative_path: str, rows: list[Any]) -> None:
        """Replace a JSONL artifact with ``rows``, creating parent directories.

        SDK records can be written directly. Use ``append_jsonl`` to extend an event
        log and ``upsert_jsonl`` to upsert a keyed candidate pool.

        Raises:
            ValueError: The path escapes the workspace.
        """
        encoded = self._dump(rows)
        atomic_write_text(self._path(relative_path), encoded)

    def append_jsonl(self, relative_path: str, rows: list[Any]) -> None:
        """Append rows to a JSONL artifact without reading or rewriting it.

        The file and parent directories are created when absent. This operation does
        not deduplicate rows; use ``upsert_jsonl`` for keyed rows.

        Raises:
            ValueError: The path escapes the workspace.
        """
        path = self._path(relative_path)
        encoded = self._dump(rows)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(encoded)

    def upsert_jsonl(self, relative_path: str, rows: list[Any], key: str = "source") -> int:
        """Upsert JSONL rows by ``key`` while preserving first-seen order.

        An absent file behaves like an empty pool. A repeated key replaces its row
        without moving it; pre-existing keyless rows are preserved.

        Returns:
            The total row count after the upsert.

        Raises:
            ValueError: A new row lacks ``key`` or the path escapes the workspace.
        """
        merged: dict[Any, Any] = {}
        path = self._path(relative_path)
        if path.is_file():
            for existing in self.read_jsonl(relative_path):
                identity = existing.get(key) if isinstance(existing, dict) else None
                merged[identity if identity is not None else object()] = existing
        for row in rows:
            if not isinstance(row, dict) or key not in row:
                shape = (
                    ", ".join(sorted(map(str, row)))
                    if isinstance(row, dict)
                    else type(row).__name__
                )
                raise ValueError(
                    f"upsert_jsonl needs a {key!r} field on every row to know what "
                    f"is the same document. Got a row with: {shape}. Pass key= the "
                    f"field you are deduplicating on, or use append_jsonl if these "
                    f"rows have no identity."
                )
            merged[row[key]] = row
        atomic_write_text(path, self._dump(list(merged.values())))
        return len(merged)

    def exists(self, relative_path: str) -> bool:
        """Return whether a workspace-relative artifact exists.

        Raises:
            ValueError: The path escapes the workspace.
        """
        return self._path(relative_path).is_file()

    def list(self, prefix: str = "") -> list[str]:
        """List sorted workspace-relative artifact paths under ``prefix``.

        Runtime files whose names start with ``.opensac-`` are hidden. An absent
        workspace returns an empty list.
        """
        workspace = self._workspace_path()
        if not workspace.exists():
            return []
        return sorted(
            relative
            for path in workspace.rglob("*")
            if path.is_file() and not path.name.startswith(".opensac-")
            for relative in [str(path.relative_to(workspace))]
            if relative.startswith(prefix)
        )

    def read_jsonl(self, relative_path: str) -> list[Any]:
        """Read non-empty JSONL lines as recursively wrapped JSON values.

        Returned objects support both ``row.field`` and ``row["field"]`` reads.

        Raises:
            FileNotFoundError: The artifact does not exist.
            ValueError: The path escapes the workspace or a line is invalid JSON.
        """
        with self._path(relative_path).open("r", encoding="utf-8") as handle:
            return [wrap(json.loads(line)) for line in handle if line.strip()]

    def write_json(self, relative_path: str, value: Any) -> None:
        """Replace a JSON artifact, creating parent directories when needed.

        SDK records and ordinary JSON values can be written directly.

        Raises:
            ValueError: The path escapes the workspace.
        """
        encoded = strict_json_dumps(value, field="value", indent=2)
        atomic_write_text(self._path(relative_path), encoded)

    def read_json(self, relative_path: str) -> Any:
        """Read a JSON artifact and recursively wrap its object values.

        Returned objects support both attribute and mapping field reads.

        Raises:
            FileNotFoundError: The artifact does not exist.
            ValueError: The path escapes the workspace or the file is invalid JSON.
        """
        return wrap(json.loads(self._path(relative_path).read_text(encoding="utf-8")))

    @classmethod
    def from_environment(cls) -> WorkspaceResource:
        return cls(None)
