from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any

from ._record import Record, record, wrap
from .transport import UnixSocketTransport


class SearchResource:
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
    ) -> list[Record]:
        """Fuse successful search batches locally with reciprocal-rank fusion."""
        parsed_batches = [record(batch) for batch in batches]
        normalized_weights = self._validate_fusion_options(
            len(parsed_batches), weights=weights, k=k, limit=limit
        )
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
                previous = best_in_batch.get(hit.ref)
                if previous is None or (hit.rank, hit_index) < (
                    previous[1].rank,
                    previous[0],
                ):
                    best_in_batch[hit.ref] = (hit_index, hit)

            for hit_index, hit in best_in_batch.values():
                source = record(
                    {
                        "batch_index": batch_index,
                        "query": batch.query,
                        "backend": hit.backend,
                        "rank": hit.rank,
                        "score": hit.get("score"),
                    }
                )
                representative_key = (hit.rank, batch_index, hit_index)
                candidate = candidates.get(hit.ref)
                if candidate is None:
                    candidates[hit.ref] = {
                        "hit": hit,
                        "representative_key": representative_key,
                        "best_rank": hit.rank,
                        "earliest_batch": batch_index,
                        "sources": [source],
                        "fused_score": weight / (k + hit.rank),
                    }
                    continue

                candidate["sources"].append(source)
                candidate["fused_score"] += weight / (k + hit.rank)
                candidate["best_rank"] = min(candidate["best_rank"], hit.rank)
                candidate["earliest_batch"] = min(candidate["earliest_batch"], batch_index)
                if representative_key < candidate["representative_key"]:
                    candidate["hit"] = hit
                    candidate["representative_key"] = representative_key

        ordered = sorted(
            candidates.items(),
            key=lambda item: (
                -item[1]["fused_score"],
                item[1]["best_rank"],
                item[1]["earliest_batch"],
                item[0],
            ),
        )
        fused = [
            record(
                {
                    **candidate["hit"],
                    "sources": candidate["sources"],
                    "fused_score": candidate["fused_score"],
                    "fused_rank": fused_rank,
                }
            )
            for fused_rank, (_, candidate) in enumerate(ordered, start=1)
        ]
        return fused if limit is None else fused[:limit]

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


class ContentResource:
    def __init__(self, transport: UnixSocketTransport) -> None:
        self._transport = transport

    def get_many(self, refs: list[str]) -> list[Record]:
        return self._transport.call("content.get_many", {"refs": refs})

    def read(
        self,
        refs: list[str],
        *,
        offset: int = 1,
        limit: int = 200,
        max_chars: int = 100_000,
    ) -> list[Record]:
        return self._transport.call(
            "content.read",
            {"refs": refs, "offset": offset, "limit": limit, "max_chars": max_chars},
        )

    def grep_report(
        self,
        refs: list[str],
        pattern: str,
        *,
        context: int = 0,
        max_matches_per_ref: int = 20,
    ) -> Record:
        return self._transport.call(
            "content.grep_report",
            {
                "refs": refs,
                "pattern": pattern,
                "context": context,
                "max_matches_per_ref": max_matches_per_ref,
            },
        )

    def passages(
        self,
        query: str,
        refs: list[str],
        *,
        limit: int = 20,
        max_per_ref: int = 3,
    ) -> Record:
        return self._transport.call(
            "content.passages",
            {
                "query": query,
                "refs": refs,
                "limit": limit,
                "max_per_ref": max_per_ref,
            },
        )


class CitationsResource:
    def __init__(self, transport: UnixSocketTransport) -> None:
        self._transport = transport

    def resolve(self, refs: list[str]) -> list[Record]:
        return self._transport.call("citations.resolve", {"refs": refs})

    def resolve_requests(self, requests: list[dict[str, Any]]) -> list[Record]:
        return self._transport.call("citations.resolve", {"requests": requests})


class LLMResource:
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
    def __init__(self, transport: UnixSocketTransport) -> None:
        self._transport = transport

    def usage(self) -> dict[str, Any]:
        return self._transport.call("session.usage", {})


class StateResource:
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
        path = self._path(relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self._dump(rows), encoding="utf-8")

    def append_jsonl(self, relative_path: str, rows: list[Any]) -> None:
        path = self._path(relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(self._dump(rows))

    def merge_jsonl(self, relative_path: str, rows: list[Any], key: str = "ref") -> int:
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
        return self._path(relative_path).is_file()

    def list(self, prefix: str = "") -> list[str]:
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
        with self._path(relative_path).open("r", encoding="utf-8") as handle:
            return [wrap(json.loads(line)) for line in handle if line.strip()]

    def write_json(self, relative_path: str, value: Any) -> None:
        path = self._path(relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(value, ensure_ascii=True, indent=2, default=str),
            encoding="utf-8",
        )

    def read_json(self, relative_path: str) -> Any:
        return wrap(json.loads(self._path(relative_path).read_text(encoding="utf-8")))

    @classmethod
    def from_environment(cls) -> StateResource:
        return cls(os.environ.get("OPENSAC_WORKSPACE", "/workspace"))


class OutputResource:
    def __init__(self, output_path: str, transport: UnixSocketTransport | None = None) -> None:
        self._output_path = Path(output_path)
        self._transport = transport

    def submit(
        self,
        output: Any,
        *,
        citations: list[dict[str, Any]] | None = None,
    ) -> None:
        requested = [self._citation(item) for item in citations or []]
        if requested:
            if self._transport is None:
                raise RuntimeError("Citation resolution requires a broker transport")
            if any("locator" in citation for citation in requested):
                resolved = self._transport.call("citations.resolve", {"requests": requested})
            else:
                resolved = self._transport.call(
                    "citations.resolve", {"refs": [item["ref"] for item in requested]}
                )
        else:
            resolved = []
        self._output_path.write_text(
            json.dumps(
                {"output": output, "citations": resolved},
                ensure_ascii=True,
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )

    @staticmethod
    def _citation(item: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(item, dict):
            raise ValueError("Every citation must be an object")
        if set(item) - {"ref", "locator"}:
            raise ValueError("Citations accept only ref and locator")
        ref = item.get("ref")
        if not isinstance(ref, str) or not ref:
            raise ValueError("Every citation must contain a search result ref")
        if "locator" in item and item["locator"] is None:
            raise ValueError("locator must be omitted when no evidence locator is available")
        if "locator" in item and not isinstance(item["locator"], dict):
            raise ValueError("locator must be an object")
        return dict(item)

    @classmethod
    def from_environment(cls, transport: UnixSocketTransport | None = None) -> OutputResource:
        return cls(
            os.environ.get("OPENSAC_OUTPUT_PATH", "/workspace/.opensac-output.json"),
            transport,
        )
