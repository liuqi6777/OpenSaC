from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ._diagnostics import (
    failure_detail,
    failure_status,
    record_external_failures,
    write_submission,
)
from ._json import atomic_write_text, strict_json_dumps, strict_jsonl_dumps
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
    ) -> list[Record]:
        """Search one query and return a ranked window of hit records.

        ``offset`` is depth in the full ranking, not a page number.
        ``include_domains`` is accepted only by backends that support filtering.

        Returns:
            Hits ordered by ``rank``. Each record includes ``source``, ``backend``,
            ``title``, ``snippet``, ``score``, ``rank``, ``retrieval``, and
            ``metadata``. An empty list is a successful search with no matches.

        Raises:
            BrokerError: The whole search failed or the request was rejected.
        """
        query = string(query, "query", strip=True)
        limit, offset = self._search_window(limit, offset)
        include_domains = optional_string_list(include_domains, "include_domains")
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
    ) -> list[Record]:
        """Search several queries with bounded broker-side concurrency.

        ``limit`` and ``offset`` define each ranked window. ``concurrency`` bounds
        simultaneous provider work; ``include_domains`` has the same semantics as
        single-query search.

        Returns:
            One outcome per input query, in input order. ``status`` is exactly
            ``"success"`` or a human-readable failure string; callers should not
            parse failure strings. Failed outcomes have empty ``hits``.

        Raises:
            BrokerError: The complete batch call failed before aligned results
                could be returned.
        """
        queries = string_list(queries, "queries")
        limit, offset = self._search_window(limit, offset)
        concurrency = integer(concurrency, "concurrency", minimum=1)
        include_domains = optional_string_list(include_domains, "include_domains")
        report = self._transport.call(
            "search.query_many",
            {
                "queries": queries,
                "limit": limit,
                "offset": offset,
                "concurrency": concurrency,
                "include_domains": include_domains,
            },
        )
        failures = [
            failure_detail(failure, input_index=failure.input_index, query=failure.query)
            for failure in report.failures
        ]
        record_external_failures(
            "search.many",
            success_count=len(report.results),
            failures=failures,
        )
        outcomes: list[Record | None] = [None] * len(queries)
        self._validate_report_size("search.many", report.input_count, len(outcomes))
        for result in report.results:
            input_index = self._claim_outcome("search.many", outcomes, result.input_index)
            outcomes[input_index] = record(
                {
                    "query": result.query,
                    "status": "success",
                    "hits": result.hits,
                }
            )
        for failure in report.failures:
            input_index = self._claim_outcome("search.many", outcomes, failure.input_index)
            outcomes[input_index] = record(
                {
                    "query": failure.query,
                    "status": failure_status(failure),
                    "hits": [],
                }
            )
        self._require_complete_report("search.many", outcomes)
        return [outcome for outcome in outcomes if outcome is not None]

    @staticmethod
    def _search_window(limit: Any, offset: Any) -> tuple[int, int]:
        return integer(limit, "limit", minimum=1), integer(offset, "offset", minimum=0)

    @staticmethod
    def _validate_report_size(method: str, reported: Any, expected: int) -> None:
        if isinstance(reported, bool) or not isinstance(reported, int) or reported != expected:
            raise SearchResource._protocol_error(method, "an invalid input_count")

    @staticmethod
    def _claim_outcome(method: str, outcomes: list[Record | None], input_index: Any) -> int:
        if (
            isinstance(input_index, bool)
            or not isinstance(input_index, int)
            or input_index < 0
            or input_index >= len(outcomes)
            or outcomes[input_index] is not None
        ):
            raise SearchResource._protocol_error(method, "invalid outcome alignment")
        return input_index

    @staticmethod
    def _require_complete_report(method: str, outcomes: list[Record | None]) -> None:
        if any(outcome is None for outcome in outcomes):
            raise SearchResource._protocol_error(method, "incomplete outcome alignment")

    @staticmethod
    def _protocol_error(method: str, detail: str) -> BrokerError:
        return BrokerError(
            f"{method} returned {detail}",
            code="broker_protocol_error",
            retryable=False,
        )

    def fuse_rrf(
        self,
        report: list[Record | dict[str, Any]],
        *,
        weights: list[float] | None = None,
        k: int = 60,
        limit: int | None = None,
        exclude_domains: list[str] | None = None,
        domain_weights: dict[str, float] | None = None,
        max_per_domain: int | None = None,
    ) -> list[Record]:
        """Fuse multi-query search outcomes locally with domain-aware RRF.

        This deterministic helper makes no broker call. ``weights`` aligns with the
        outcome order, including failed ones; provenance derives ``input_index``
        from that order. ``k`` controls rank smoothing and ``limit`` truncates
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
        if not isinstance(report, list):
            raise ValueError("report must be the list returned by search.many")
        parsed_batches: list[Record] = []
        for outcome in report:
            if not isinstance(outcome, dict):
                raise ValueError("Every search outcome must be a mapping")
            parsed_batches.append(record(outcome))
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
            status = batch.get("status")
            if not isinstance(status, str):
                raise ValueError("Every search outcome status must be a string")
            if not isinstance(batch.get("query"), str):
                raise ValueError("Every search outcome query must be a string")
            if not isinstance(batch.get("hits"), list):
                raise ValueError("Every search outcome hits must be a list")
            if status != "success":
                continue
            weight = normalized_weights[input_index]
            best_in_batch: dict[str, tuple[int, Record]] = {}
            for hit_index, hit in enumerate(batch.hits):
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
                        "query": batch.query,
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
    separate logical requests. Single-source failures raise ``BrokerError``; collection tasks
    retain per-source failures in aligned outcomes or reports.
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

    def fetch(self, source: str) -> Record:
        """Fetch one complete normalized document for selected-source local processing.

        Returns:
            A document containing ``source``, ``title``, ``date``, ``text``, and
            provider-owned ``metadata``. Reuse it locally for several checks, optionally persist
            one copy with ``sdk.state``, and never print the complete text.

        Raises:
            BrokerError: The source could not be fetched.
        """
        return self._transport.call("content.fetch", {"source": self._source(source)})

    def read(
        self,
        source: str,
        *,
        start_line: int = 1,
        start_character: int = 0,
        line_count: int = 200,
        max_chars: int = 100_000,
    ) -> Record:
        """Read one 1-indexed line window from a source.

        Lines are 1-based and characters are 0-based. ``line_count`` bounds logical
        lines and ``max_chars`` bounds returned text. Pass ``window.next`` back as
        ``start_line`` and ``start_character`` to continue without losing text. In a
        fetch-first workflow, local slicing usually avoids this additional logical request.

        Returns:
            One content slice with provider ``metadata`` and a separate ``window``.

        Raises:
            BrokerError: The broker could not return a typed content row.
        """
        start_line = integer(start_line, "start_line", minimum=1)
        start_character = integer(start_character, "start_character", minimum=0)
        line_count = integer(line_count, "line_count", minimum=1)
        max_chars = integer(max_chars, "max_chars", minimum=1)
        return self._transport.call(
            "content.read",
            {
                "source": self._source(source),
                "start_line": start_line,
                "start_character": start_character,
                "line_count": line_count,
                "max_chars": max_chars,
            },
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
    ) -> list[Record]:
        """Search document lines and preserve per-source failures.

        ``mode`` selects regular-expression or literal matching. ``start_line`` is
        1-based, ``context_lines`` adds surrounding lines, and ``limit_per_source``
        bounds each document's contribution. In a fetch-first workflow, local matching
        usually avoids this additional logical request.

        Returns:
            One outcome per source, in input order. ``status`` is exactly
            ``"success"`` or a human-readable failure string. Each outcome owns
            its ``matches``; a non-null ``next_start_line`` continues a capped scan.

        Raises:
            BrokerError: The report could not be produced.
        """
        pattern = string(pattern, "pattern")
        if mode not in {"regex", "literal"}:
            raise ValueError("mode must be 'regex' or 'literal'")
        case_sensitive = boolean(case_sensitive, "case_sensitive")
        start_line = integer(start_line, "start_line", minimum=1)
        context_lines = integer(context_lines, "context_lines", minimum=0)
        limit_per_source = integer(limit_per_source, "limit_per_source", minimum=1)
        sources = self._sources(sources)
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

        outcomes: list[Record | None] = [None] * len(sources)
        for result in report.source_results:
            input_index = SearchResource._claim_outcome(
                "content.grep", outcomes, result.input_index
            )
            outcomes[input_index] = record(
                {
                    "source": result.source,
                    "title": result.title,
                    "status": "success",
                    "matches": matches_by_input[input_index],
                    "next_start_line": result.next_start_line,
                }
            )
        for failure in report.failures:
            input_index = SearchResource._claim_outcome(
                "content.grep", outcomes, failure.input_index
            )
            if matches_by_input[input_index]:
                raise SearchResource._protocol_error("content.grep", "matches for a failed source")
            outcomes[input_index] = record(
                {
                    "source": failure.source,
                    "title": None,
                    "status": failure_status(failure),
                    "matches": [],
                    "next_start_line": None,
                }
            )
        SearchResource._require_complete_report("content.grep", outcomes)
        return [outcome for outcome in outcomes if outcome is not None]

    def passages(
        self,
        query: str,
        *,
        sources: list[str],
        limit: int = 20,
        limit_per_source: int = 3,
    ) -> Record:
        """Rank passages across a caller-supplied source set.

        The broker deduplicates sources in first-seen order, ranks successful documents
        together, then applies ``limit_per_source``. ``limit`` bounds the report.
        Scores are comparable only within this report. This semantic ranking can add value
        after fetch when ordinary local lexical checks are insufficient.

        Returns:
            A report with ``query``, ``passages``, fetch ``failures``, reranker
            ``warnings``, ``input_count``, and ``unique_source_count``. Each passage
            includes exact ``text``, coordinates, and ranker metadata.

        Raises:
            BrokerError: The report could not be produced.
        """
        query = string(query, "query", strip=True)
        limit = integer(limit, "limit", minimum=1)
        limit_per_source = integer(limit_per_source, "limit_per_source", minimum=1)
        report = self._transport.call(
            "content.passages",
            {
                "query": query,
                "sources": self._sources(sources),
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
    ) -> str:
        """Run one free-form pipeline-model completion.

        ``system`` supplies optional instructions, ``temperature`` controls sampling,
        and ``max_tokens`` optionally bounds the response. Prefer ``extract``
        when downstream code expects structured data.

        Returns:
            The model's response text.

        Raises:
            BrokerError: The deployment has no pipeline model or completion fails.
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
        return self._transport.call(
            "llm.complete",
            {
                "prompt": prompt,
                "system": system,
                "temperature": temperature,
                "max_tokens": max_tokens,
            },
        )

    def extract(
        self,
        item: Any,
        *,
        instruction: str,
        schema: dict[str, Any],
        max_tokens: int | None = None,
        repair_attempts: int = 0,
    ) -> Record:
        """Transform one JSON value into a schema-checked JSON object.

        ``item`` and ``schema`` must be strict-JSON serializable. The schema root
        must be an object. ``repair_attempts`` must be non-negative and cannot
        exceed the broker-advertised limit.

        Returns:
            The validated JSON object directly.

        Raises:
            ValueError: Local arguments are not JSON serializable or valid.
            BrokerError: Provider, JSON, schema, repair, or quota processing fails.
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

        params = {
            "item": item,
            "instruction": instruction,
            "schema": schema,
            "repair_attempts": repair_attempts,
        }
        if max_tokens is not None:
            params["max_tokens"] = max_tokens
        return self._transport.call("llm.extract", params)

    @staticmethod
    def _ensure_json_serializable(value: Any, field: str) -> None:
        strict_json_dumps(value, field=field)


class SessionResource:
    """Inspect strategy usage and remaining hard allowances for this session."""

    def __init__(self, transport: UnixSocketTransport) -> None:
        self._transport = transport

    def usage(self) -> Record:
        """Return current capability spend, remaining budgets, and terminal state.

        Returns:
            Core logical counters, reserved output tokens, sandbox/workspace use,
            ``budget_remaining``, and ``terminal_reason``.

        Raises:
            BrokerError: Session usage cannot be read.
        """
        return self._transport.call("session.usage", {})

    def capabilities(self) -> Record:
        """Return session-visible capabilities, limits, and contract versions.

        The record reflects the active backend and this session's mechanism
        switches without exposing provider credentials or internal endpoints.
        """
        return self._transport.call("session.capabilities", {})


class StateResource:
    """Persist JSON and JSONL artifacts across executions in one live session.

    Paths are workspace-relative and cannot escape the session workspace. State is
    program memory, not a database; local document sources become invalid if the
    host reports ``state_lost``. Public web URLs remain meaningful across sessions.
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
            raise ValueError("State path must remain inside the session workspace")
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
        not deduplicate rows; use ``upsert_jsonl`` for keyed state.

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
        """Return whether a workspace-relative state file exists.

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
    def from_environment(cls) -> StateResource:
        return cls(None)


class OutputResource:
    """Submit the final structured result and optional source strings."""

    def __init__(self, output_path: str | None) -> None:
        self._output_path = Path(output_path) if output_path is not None else None

    def _path(self) -> Path:
        if self._output_path is not None:
            return self._output_path
        return Path(os.environ.get("OPENSAC_OUTPUT_PATH", "/workspace/.opensac-output.json"))

    def submit(
        self,
        value: Any,
        *,
        citations: list[str] | None = None,
    ) -> None:
        """Write the final output artifact with optional URL/source labels.

        Citations are unverified source declarations. They are not resolved by the
        broker and do not claim that OpenSAC validated a source against the answer.

        Raises:
            ValueError: Citations are malformed or exceed the local bound.
        """
        if citations is not None and not isinstance(citations, list):
            raise ValueError("citations must be a list of source strings")
        if citations is not None and len(citations) > 256:
            raise ValueError("citations must contain at most 256 source strings")
        sources = [self._citation(item, index) for index, item in enumerate(citations or [])]
        write_submission(self._path(), value, sources)

    @staticmethod
    def _citation(item: Any, input_index: int) -> str:
        if not isinstance(item, str):
            raise ValueError(f"citation at input index {input_index} must be a string")
        source = item.strip()
        if not source:
            raise ValueError(f"citation at input index {input_index} must not be empty")
        if len(source) > 4096:
            raise ValueError(
                f"citation at input index {input_index} must be at most 4096 characters"
            )
        return source

    @classmethod
    def from_environment(cls) -> OutputResource:
        return cls(None)
