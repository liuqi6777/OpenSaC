from __future__ import annotations

import json
import math
import os
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ._record import Record, record, wrap
from .transport import UnixSocketTransport


class SearchResource:
    """Find documents and combine ranked search result sets.

    Call ``sdk.search(query, limit=10, offset=0, domains=None)`` for one ranked
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
        domains: list[str] | None = None,
    ) -> list[Record]:
        """Search one query and return a ranked window of hit records.

        ``offset`` is depth in the full ranking, not a page number. ``domains``
        is accepted only by backends that support domain filtering.

        Returns:
            Hits ordered by ``rank``. Each record includes ``source``, ``backend``,
            ``title``, ``snippet``, ``score``, ``rank``, ``retrieval``, and
            ``metadata``. An empty list is a successful search with no matches.

        Raises:
            BrokerError: The whole search failed or the request was rejected.
        """
        return self._transport.call(
            "search.query",
            {"query": query, "limit": limit, "offset": offset, "domains": domains},
        )

    def many(
        self,
        queries: list[str],
        *,
        limit_per_query: int = 10,
        offset: int = 0,
        concurrency: int = 5,
        domains: list[str] | None = None,
    ) -> list[Record]:
        """Search several queries with bounded broker-side concurrency.

        ``limit_per_query`` and ``offset`` define each ranked window.
        ``concurrency`` bounds simultaneous provider work; ``domains`` has the
        same backend-dependent semantics as single-query search.

        Returns:
            One batch per input query, in input order. A batch contains ``query``,
            ``hits``, and ``failure``. A per-query failure has no hits; inspect its
            ``code``, ``message``, ``retryable``, and attempt metadata. Empty hits
            with ``failure is None`` are successful.

        Raises:
            BrokerError: The complete batch call failed before aligned results
                could be returned.
        """
        return self._transport.call(
            "search.query_many",
            {
                "queries": queries,
                "limit_per_query": limit_per_query,
                "offset": offset,
                "concurrency": concurrency,
                "domains": domains,
            },
        )

    def fuse_rrf(
        self,
        batches: list[Record | dict[str, Any]],
        *,
        weights: list[float] | None = None,
        k: int = 60,
        limit: int | None = None,
        exclude_domains: list[str] | None = None,
        domain_weights: dict[str, float] | None = None,
        max_per_domain: int | None = None,
    ) -> list[Record]:
        """Fuse successful search batches locally with domain-aware RRF.

        This deterministic helper makes no broker call. Failed batches are skipped
        and remain available to the caller through their original ``failure``
        fields. ``weights`` must align one-to-one with ``batches``; ``k`` controls
        rank smoothing and ``limit`` truncates the fused list. Domain policies match
        an exact hostname or any of its subdomains. ``exclude_domains`` removes
        candidates, ``domain_weights`` multiplies their RRF scores, and
        ``max_per_domain`` caps candidates sharing one exact hostname before the
        final limit is applied. Sources that are not web URLs are unaffected.

        Returns:
            Search-hit records extended with ``provenance``, ``raw_fused_score``,
            ``domain_weight``, ``fused_score``, and 1-based ``fused_rank``. Document
            sources and metadata are preserved.

        Raises:
            ValueError: Weights, domains, ranks, ``k``, or limits are invalid.
        """
        parsed_batches = [record(batch) for batch in batches]
        normalized_weights = self._validate_fusion_options(
            len(parsed_batches), weights=weights, k=k, limit=limit
        )
        normalized_exclusions = self._normalize_domain_list(
            exclude_domains, option="exclude_domains"
        )
        normalized_domain_weights = self._normalize_domain_weights(domain_weights)
        self._validate_max_per_domain(max_per_domain)
        candidates: dict[str, dict[str, Any]] = {}

        for batch_index, (batch, weight) in enumerate(
            zip(parsed_batches, normalized_weights, strict=True)
        ):
            if batch.get("failure") is not None:
                continue
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
                        "batch_index": batch_index,
                        "query": batch.query,
                        "backend": hit.backend,
                        "rank": hit.rank,
                        "score": hit.get("score"),
                    }
                )
                representative_key = (hit.rank, batch_index, hit_index)
                candidate = candidates.get(hit.source)
                if candidate is None:
                    candidates[hit.source] = {
                        "hit": hit,
                        "representative_key": representative_key,
                        "best_rank": hit.rank,
                        "earliest_batch": batch_index,
                        "provenance": [provenance],
                        "fused_score": weight / (k + hit.rank),
                        "domain": self._source_domain(hit.source),
                    }
                    continue

                candidate["provenance"].append(provenance)
                candidate["fused_score"] += weight / (k + hit.rank)
                candidate["best_rank"] = min(candidate["best_rank"], hit.rank)
                candidate["earliest_batch"] = min(candidate["earliest_batch"], batch_index)
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
                item[1]["earliest_batch"],
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

    Prefer ``passages`` for semantic discovery, ``grep_report`` for exact text,
    and ``read`` for deliberate line-window expansion. Content operations report
    partial fetch failures instead of silently dropping unreadable sources.
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
            if len(source) > 4096:
                raise ValueError(
                    f"source at input index {input_index} must be at most 4096 characters"
                )
            validated.append(source)
        return validated

    def get_many(self, sources: list[str]) -> list[Record]:
        """Fetch complete normalized documents for advanced local processing.

        Returns:
            One content row per input source, in input order. A successful row contains
            ``text`` and source metadata; an unreadable row has empty text and a
            structured ``failure``. Prefer narrower content operations when possible.

        Raises:
            BrokerError: The whole request failed or every failure was systemic.
        """
        return self._transport.call("content.get_many", {"sources": self._sources(sources)})

    def read(
        self,
        sources: list[str],
        *,
        offset: int = 1,
        limit: int = 200,
        max_chars: int = 100_000,
    ) -> list[Record]:
        """Read the same 1-indexed line window from each referenced document.

        ``offset`` is the first line and ``limit`` bounds line count; ``max_chars``
        also bounds unusually long lines. Use ``metadata.next_offset`` to continue.

        Returns:
            Source-aligned content rows. ``metadata`` includes ``start_line``,
            ``end_line``, ``total_lines``, and ``next_offset``. Inspect ``failure``
            on unreadable rows.

        Raises:
            BrokerError: The whole request failed or every failure was systemic.
        """
        return self._transport.call(
            "content.read",
            {
                "sources": self._sources(sources),
                "offset": offset,
                "limit": limit,
                "max_chars": max_chars,
            },
        )

    def grep_report(
        self,
        sources: list[str],
        pattern: str,
        *,
        context: int = 0,
        max_matches_per_source: int = 20,
    ) -> Record:
        """Search document lines and preserve per-source fetch failures.

        ``pattern`` is a case-insensitive regular expression; malformed regex is
        treated literally. ``context`` adds surrounding lines and
        ``max_matches_per_source`` bounds each document's contribution. Match line
        numbers are 1-indexed and can be passed directly to ``read``.

        Returns:
            A report with ``matches``, ``failures``, and ``input_count``. Each match
            includes source metadata, ``line``, ``text``, context, and
            ``input_index``. Zero matches is success.

        Raises:
            BrokerError: The report could not be produced.
        """
        return self._transport.call(
            "content.grep_report",
            {
                "sources": self._sources(sources),
                "pattern": pattern,
                "context": context,
                "max_matches_per_source": max_matches_per_source,
            },
        )

    def passages(
        self,
        query: str,
        sources: list[str],
        *,
        limit: int = 20,
        max_per_source: int = 3,
    ) -> Record:
        """Rank passages across a caller-supplied set of sources.

        The broker deduplicates sources in first-seen order, ranks successful documents
        together, then applies ``max_per_source``. ``limit`` bounds the whole report.
        Scores are comparable only within this report.

        Returns:
            A report with ``query``, ``passages``, ``failures``, ``input_count``, and
            ``unique_source_count``. Each passage includes exact ``text``, coordinates,
            and ranker metadata.

        Raises:
            BrokerError: The report could not be produced.
        """
        return self._transport.call(
            "content.passages",
            {
                "query": query,
                "sources": self._sources(sources),
                "limit": limit,
                "max_per_source": max_per_source,
            },
        )


class LLMResource:
    """Use the optional pipeline model for bounded semantic subroutines.

    Prefer deterministic Python whenever it is sufficient. Use ``extract_many`` for
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
        and ``max_tokens`` optionally bounds the response. Prefer ``extract_many``
        when downstream code expects structured data.

        Returns:
            The model's response text.

        Raises:
            BrokerError: The deployment has no pipeline model or completion fails.
        """
        return self._transport.call(
            "llm.complete",
            {
                "prompt": prompt,
                "system": system,
                "temperature": temperature,
                "max_tokens": max_tokens,
            },
        )

    def complete_many(
        self,
        prompts: list[str],
        *,
        system: str | None = None,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        concurrency: int = 4,
    ) -> list[str]:
        """Run aligned free-form completions with bounded concurrency.

        ``system``, ``temperature``, and ``max_tokens`` apply to every prompt.
        ``concurrency`` bounds simultaneous model requests.

        Returns:
            One response string per prompt, in input order.

        Raises:
            BrokerError: The deployment has no pipeline model or the batch fails.
        """
        return self._transport.call(
            "llm.complete_many",
            {
                "prompts": prompts,
                "system": system,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "concurrency": concurrency,
            },
        )

    def extract_many(
        self,
        items: list[Any],
        *,
        instruction: str,
        schema: dict[str, Any],
        concurrency: int = 4,
        max_tokens: int | None = None,
        repair_attempts: int = 0,
    ) -> list[Record]:
        """Map items to a caller-defined JSON object schema.

        ``schema`` and every item must be JSON serializable. The schema root must be
        an object. ``repair_attempts`` is 0 or 1; results remain aligned even when an
        individual item does not satisfy the schema. ``instruction`` and ``schema``
        apply to every item, while ``concurrency`` and ``max_tokens`` bound execution.

        Returns:
            Rows containing ``index``, ``data``, ``error``, and ``attempts``. Exactly
            one of ``data`` or ``error`` is present for each row.

        Raises:
            ValueError: Local arguments are not JSON serializable or valid.
            BrokerError: Extraction infrastructure fails for the whole call.
        """
        if repair_attempts not in {0, 1}:
            raise ValueError("repair_attempts must be 0 or 1")
        if not isinstance(schema, dict):
            raise ValueError("schema must be a JSON-serializable object")
        self._ensure_json_serializable(schema, "schema")
        if not isinstance(items, list):
            raise ValueError("items must be a list")
        for index, item in enumerate(items):
            self._ensure_json_serializable(item, f"items[{index}]")

        params = {
            "items": items,
            "instruction": instruction,
            "schema": schema,
            "concurrency": concurrency,
            "repair_attempts": repair_attempts,
        }
        if max_tokens is not None:
            params["max_tokens"] = max_tokens
        return self._transport.call("llm.extract_many", params)

    @staticmethod
    def _ensure_json_serializable(value: Any, field: str) -> None:
        try:
            json.dumps(value, allow_nan=False)
        except (TypeError, ValueError, OverflowError, RecursionError) as exc:
            raise ValueError(f"{field} must be JSON serializable: {exc}") from exc


class SessionResource:
    """Inspect strategy usage and remaining hard allowances for this session."""

    def __init__(self, transport: UnixSocketTransport) -> None:
        self._transport = transport

    def usage(self) -> dict[str, Any]:
        """Return current capability spend, remaining budgets, and terminal state.

        Returns:
            A record containing ``exec_calls``, ``search_calls``,
            ``content_fetches``, ``llm_calls``, ``pipeline_model_tokens``,
            ``documents_seen``, ``budget_remaining``, and ``terminal_reason``.

        Raises:
            BrokerError: Session usage cannot be read.
        """
        return self._transport.call("session.usage", {})


class StateResource:
    """Persist JSON and JSONL artifacts across executions in one live session.

    Paths are workspace-relative and cannot escape the session workspace. State is
    program memory, not a database; local document sources become invalid if the
    host reports ``state_lost``. Public web URLs remain meaningful across sessions.
    """

    def __init__(self, workspace: str) -> None:
        self._workspace = Path(workspace).resolve()

    def _path(self, relative_path: str) -> Path:
        path = (self._workspace / relative_path).resolve()
        if not path.is_relative_to(self._workspace):
            raise ValueError("State path must remain inside the session workspace")
        return path

    @staticmethod
    def _dump(rows: list[Any]) -> str:
        return "".join(json.dumps(row, ensure_ascii=True, default=str) + "\n" for row in rows)

    def write_jsonl(self, relative_path: str, rows: list[Any]) -> None:
        """Replace a JSONL artifact with ``rows``, creating parent directories.

        SDK records can be written directly. Use ``append_jsonl`` to extend an event
        log and ``merge_jsonl`` to upsert a keyed candidate pool.

        Raises:
            ValueError: The path escapes the workspace.
        """
        path = self._path(relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self._dump(rows), encoding="utf-8")

    def append_jsonl(self, relative_path: str, rows: list[Any]) -> None:
        """Append rows to a JSONL artifact without reading or rewriting it.

        The file and parent directories are created when absent. This operation does
        not deduplicate rows; use ``merge_jsonl`` for keyed state.

        Raises:
            ValueError: The path escapes the workspace.
        """
        path = self._path(relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(self._dump(rows))

    def merge_jsonl(self, relative_path: str, rows: list[Any], key: str = "source") -> int:
        """Upsert JSONL rows by ``key`` while preserving first-seen order.

        An absent file behaves like an empty pool. A repeated key replaces its row
        without moving it; pre-existing keyless rows are preserved.

        Returns:
            The total row count after the merge.

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
                    f"merge_jsonl needs a {key!r} field on every row to know what "
                    f"is the same document. Got a row with: {shape}. Pass key= the "
                    f"field you are deduplicating on, or use append_jsonl if these "
                    f"rows have no identity."
                )
            merged[row[key]] = row
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self._dump(list(merged.values())), encoding="utf-8")
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
        if not self._workspace.exists():
            return []
        return sorted(
            relative
            for path in self._workspace.rglob("*")
            if path.is_file() and not path.name.startswith(".opensac-")
            for relative in [str(path.relative_to(self._workspace))]
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
        path = self._path(relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(value, ensure_ascii=True, indent=2, default=str),
            encoding="utf-8",
        )

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
        return cls(os.environ.get("OPENSAC_WORKSPACE", "/workspace"))


class OutputResource:
    """Submit the final structured result and optional source strings."""

    def __init__(self, output_path: str) -> None:
        self._output_path = Path(output_path)

    def submit(
        self,
        output: Any,
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
        encoded = json.dumps(
            {"output": output, "citations": sources},
            ensure_ascii=True,
            indent=2,
            default=str,
        )
        self._output_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            dir=self._output_path.parent,
            prefix=f".{self._output_path.name}.",
            suffix=".tmp",
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(encoded)
            os.replace(temporary, self._output_path)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temporary)

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
        return cls(os.environ.get("OPENSAC_OUTPUT_PATH", "/workspace/.opensac-output.json"))
