from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

from openai import AsyncOpenAI
from opensac_sdk.models import ContentSnippet, SearchBatch, SearchHit

from opensac.backends.base import SearchBackend
from opensac.broker.policy import CapabilityPolicy
from opensac.models import CapabilityEvent, Session

_EVENT_MODEL_TOKENS: ContextVar[int] = ContextVar(
    "opensac_event_model_tokens", default=0
)


@dataclass
class BrokerSession:
    session: Session
    policy: CapabilityPolicy
    references: dict[str, SearchHit] = field(default_factory=dict)
    traces: dict[str, list[CapabilityEvent]] = field(default_factory=dict)
    trace_sequence: int = 0

    def next_trace_sequence(self) -> int:
        self.trace_sequence += 1
        return self.trace_sequence


class BrokerService:
    def __init__(
        self,
        backends: dict[str, SearchBackend],
        *,
        model_client: AsyncOpenAI | None = None,
        extraction_model: str = "",
        max_concurrency: int = 12,
    ) -> None:
        self.backends = backends
        self.model_client = model_client
        self.extraction_model = extraction_model
        self.sessions: dict[str, BrokerSession] = {}
        self._semaphore = asyncio.Semaphore(max_concurrency)

    def register_session(self, session: Session, *, token: str | None = None) -> BrokerSession:
        state = BrokerSession(
            session=session,
            policy=CapabilityPolicy(session.limits, set(session.backends)),
        )
        self.sessions[token or session.token] = state
        return state

    def unregister_session(self, token: str) -> None:
        self.sessions.pop(token, None)

    async def call(
        self,
        token: str,
        method: str,
        params: dict[str, Any],
        *,
        execution_id: str | None = None,
    ) -> Any:
        state = self.sessions.get(token)
        if state is None:
            raise PermissionError("Unknown or expired session token")
        handlers: dict[str, Callable[[BrokerSession, dict[str, Any]], Awaitable[Any]]] = {
            "search.web": self._search_web,
            "search.local": self._search_local,
            "search.web_many": self._search_web_many,
            "search.local_many": self._search_local_many,
            "content.get_many": self._content_get_many,
            "content.snippets": self._content_snippets,
            "citations.resolve": self._resolve_citations,
            "llm.complete": self._complete,
            "llm.complete_many": self._complete_many,
            "llm.extract_many": self._extract_many,
        }
        handler = handlers.get(method)
        if handler is None:
            raise ValueError(f"Unsupported capability: {method}")
        sequence = state.next_trace_sequence()
        started = time.monotonic()
        token_context = _EVENT_MODEL_TOKENS.set(0)
        try:
            result = await handler(state, params)
        except Exception as exc:
            self._append_trace(
                state,
                execution_id,
                CapabilityEvent(
                    sequence=sequence,
                    method=method,
                    status="error",
                    duration_seconds=time.monotonic() - started,
                    queries=self._trace_queries(method, params),
                    input_count=self._trace_input_count(method, params),
                    model_tokens=_EVENT_MODEL_TOKENS.get(),
                    error_type=type(exc).__name__,
                ),
            )
            raise
        else:
            self._append_trace(
                state,
                execution_id,
                CapabilityEvent(
                    sequence=sequence,
                    method=method,
                    status="ok",
                    duration_seconds=time.monotonic() - started,
                    queries=self._trace_queries(method, params),
                    input_count=self._trace_input_count(method, params),
                    result_count=self._trace_result_count(method, result),
                    model_tokens=_EVENT_MODEL_TOKENS.get(),
                ),
            )
            return result
        finally:
            _EVENT_MODEL_TOKENS.reset(token_context)

    @staticmethod
    def _append_trace(
        state: BrokerSession,
        execution_id: str | None,
        event: CapabilityEvent,
    ) -> None:
        if execution_id:
            state.traces.setdefault(execution_id, []).append(event)

    def take_trace(self, token: str, execution_id: str | None) -> list[CapabilityEvent]:
        if not execution_id:
            return []
        state = self.sessions.get(token)
        if state is None:
            return []
        return state.traces.pop(execution_id, [])

    @staticmethod
    def _trace_queries(method: str, params: dict[str, Any]) -> list[str]:
        if not method.startswith("search."):
            return []
        if method.endswith("_many"):
            return [str(item) for item in params.get("queries", [])]
        query = str(params.get("query", ""))
        return [query] if query else []

    @staticmethod
    def _trace_input_count(method: str, params: dict[str, Any]) -> int:
        if method.startswith("search."):
            return len(params.get("queries", [])) if method.endswith("_many") else 1
        if method.startswith("content.") or method == "citations.resolve":
            return len(params.get("refs", []))
        if method in {"llm.complete_many", "llm.extract_many"}:
            key = "prompts" if method == "llm.complete_many" else "items"
            return len(params.get(key, []))
        return 1

    @staticmethod
    def _trace_result_count(method: str, result: Any) -> int:
        if method.startswith("search.") and method.endswith("_many"):
            return sum(len(batch.get("hits", [])) for batch in result)
        if isinstance(result, list):
            return len(result)
        return 1 if result is not None else 0

    async def _search_web(
        self, state: BrokerSession, params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        return await self._search(state, "web", params)

    async def _search_local(
        self, state: BrokerSession, params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        return await self._search(state, "local", params)

    async def _search(
        self,
        state: BrokerSession,
        backend_name: str,
        params: dict[str, Any],
    ) -> list[dict[str, Any]]:
        state.policy.require_backend(backend_name)
        await state.policy.consume_search()
        backend = self.backends.get(backend_name)
        if backend is None:
            raise RuntimeError(f"Backend '{backend_name}' is not configured")
        query = str(params.get("query", "")).strip()
        if not query:
            raise ValueError("query must not be empty")
        limit = min(max(int(params.get("limit", 10)), 1), 100)
        async with self._semaphore:
            hits = await backend.search(
                query,
                limit=limit,
                domains=params.get("domains"),
            )
        for hit in hits:
            hit.ref = f"ref_{uuid.uuid4().hex}"
            state.references[hit.ref] = hit
        return [hit.model_dump(mode="json") for hit in hits]

    async def _search_web_many(
        self, state: BrokerSession, params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        return await self._search_many(state, "web", params)

    async def _search_local_many(
        self, state: BrokerSession, params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        return await self._search_many(state, "local", params)

    async def _search_many(
        self,
        state: BrokerSession,
        backend_name: str,
        params: dict[str, Any],
    ) -> list[dict[str, Any]]:
        queries = [str(query) for query in params.get("queries", [])]
        concurrency = min(max(int(params.get("concurrency", 5)), 1), 20)
        gate = asyncio.Semaphore(concurrency)

        async def one(query: str) -> SearchBatch:
            async with gate:
                try:
                    hits = await self._search(
                        state,
                        backend_name,
                        {"query": query, "limit": params.get("limit_per_query", 10)},
                    )
                    return SearchBatch(query=query, hits=hits)
                except Exception as exc:
                    return SearchBatch(query=query, error=str(exc))

        batches = await asyncio.gather(*(one(query) for query in queries))
        # Partial failures stay in batch.error so the program can degrade
        # gracefully, but a wholesale failure (missing backend, bad credentials,
        # rate limit) must not be reported as an empty result set.
        failed = [batch for batch in batches if batch.error]
        if batches and len(failed) == len(batches):
            raise RuntimeError(
                f"All {len(batches)} '{backend_name}' searches failed: {failed[0].error}"
            )
        return [batch.model_dump(mode="json") for batch in batches]

    def _resolve_refs(self, state: BrokerSession, refs: list[str]) -> list[SearchHit]:
        missing = [ref for ref in refs if ref not in state.references]
        if missing:
            raise ValueError(f"Unknown references: {', '.join(missing[:3])}")
        return [state.references[ref] for ref in refs]

    async def _resolve_citations(
        self,
        state: BrokerSession,
        params: dict[str, Any],
    ) -> list[dict[str, Any]]:
        hits = self._resolve_refs(state, params.get("refs", []))
        return [
            {
                "ref": hit.ref,
                "title": hit.title,
                "url": hit.url,
                "docid": hit.docid,
                "evidence": hit.snippet,
                "backend": hit.backend,
            }
            for hit in hits
        ]

    async def _content_get_many(
        self,
        state: BrokerSession,
        params: dict[str, Any],
    ) -> list[dict[str, Any]]:
        hits = self._resolve_refs(state, params.get("refs", []))
        return await self._fetch_content(state, hits, query=None)

    async def _content_snippets(
        self,
        state: BrokerSession,
        params: dict[str, Any],
    ) -> list[dict[str, Any]]:
        hits = self._resolve_refs(state, params.get("refs", []))
        query = str(params.get("query", ""))
        rows = await self._fetch_content(state, hits, query=query)
        per_page_chars = max(int(params.get("max_tokens_per_page", 1000)), 1) * 4
        total_chars = max(int(params.get("max_tokens", 4000)), 1) * 4
        used = 0
        for row in rows:
            text, metadata = self._select_passage(row["text"], query, per_page_chars)
            row["text"] = text
            row["metadata"] = {**row.get("metadata", {}), **metadata}
            if used + len(row["text"]) > total_chars:
                row["text"] = row["text"][: max(total_chars - used, 0)]
                row["metadata"]["truncated_by_total_budget"] = True
            used += len(row["text"])
        return [row for row in rows if row["text"]]

    @staticmethod
    def _normalize_text(text: str) -> str:
        normalized = (text or "").replace("\r\n", "\n").replace("\r", "\n")
        return re.sub(r"\n{3,}", "\n\n", normalized).strip()

    @staticmethod
    def _word_tokens(text: str) -> list[str]:
        return re.findall(r"\w+", (text or "").lower())

    @classmethod
    def _score_passage(cls, passage: str, query: str) -> float:
        normalized_passage = passage.strip()
        normalized_query = query.strip()
        if not normalized_passage or not normalized_query:
            return 0.0
        query_tokens = set(cls._word_tokens(normalized_query))
        passage_tokens = set(cls._word_tokens(normalized_passage))
        overlap_recall = (
            sum(1 for token in query_tokens if token in passage_tokens)
            / max(1, len(query_tokens))
        )
        char_similarity = SequenceMatcher(
            None,
            normalized_query.lower(),
            normalized_passage.lower(),
        ).ratio()
        return overlap_recall * 0.85 + char_similarity * 0.15

    @classmethod
    def _select_passage(
        cls,
        text: str,
        query: str,
        max_chars: int,
    ) -> tuple[str, dict[str, Any]]:
        normalized = cls._normalize_text(text)
        if not normalized:
            return "", {"passage_score": 0.0}
        paragraphs = [
            part.strip() for part in re.split(r"\n\s*\n+", normalized) if part.strip()
        ]
        if not paragraphs or not query.strip():
            return normalized[:max_chars], {
                "passage_index": 0,
                "passage_score": 0.0,
                "passage_start": 0,
                "passage_end": min(len(normalized), max_chars),
            }

        positions: list[tuple[int, int]] = []
        scores: list[float] = []
        start = 0
        for paragraph in paragraphs:
            positions.append((start, start + len(paragraph)))
            scores.append(cls._score_passage(paragraph, query))
            start += len(paragraph) + 2
        best_index = max(range(len(paragraphs)), key=scores.__getitem__)
        best_start, best_end = positions[best_index]
        remaining = max(0, max_chars - len(paragraphs[best_index]))
        window_start = max(0, best_start - remaining // 2)
        window_end = best_end + remaining // 2
        selected_indexes = [
            index
            for index, (paragraph_start, paragraph_end) in enumerate(positions)
            if index == best_index
            or (paragraph_start >= window_start and paragraph_end <= window_end)
        ]
        snippet = cls._normalize_text(
            "\n\n".join(paragraphs[index] for index in selected_indexes)
        )[:max_chars]
        selected_start = positions[selected_indexes[0]][0]
        selected_end = min(
            positions[selected_indexes[-1]][1],
            selected_start + len(snippet),
        )
        return snippet, {
            "passage_index": best_index,
            "passage_score": scores[best_index],
            "passage_start": selected_start,
            "passage_end": selected_end,
        }

    async def _fetch_content(
        self,
        state: BrokerSession,
        hits: list[SearchHit],
        *,
        query: str | None,
    ) -> list[dict[str, Any]]:
        grouped: dict[str, list[SearchHit]] = {}
        for hit in hits:
            state.policy.require_backend(hit.backend)
            grouped.setdefault(hit.backend, []).append(hit)
        await state.policy.record_content_fetches(len(hits))

        async def fetch(name: str, backend_hits: list[SearchHit]) -> list[ContentSnippet]:
            backend = self.backends.get(name)
            if backend is None:
                raise RuntimeError(f"Backend '{name}' is not configured")
            async with self._semaphore:
                return await backend.content(backend_hits, query=query)

        chunks = await asyncio.gather(*(fetch(name, rows) for name, rows in grouped.items()))
        return [item.model_dump(mode="json") for chunk in chunks for item in chunk]

    async def _chat(
        self,
        prompt: str,
        *,
        system: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_object: bool = False,
    ) -> tuple[str, int]:
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        options: dict[str, Any] = {}
        if temperature is not None:
            options["temperature"] = temperature
        if max_tokens is not None:
            options["max_completion_tokens"] = max_tokens
        if json_object:
            options["response_format"] = {"type": "json_object"}
        async with self._semaphore:
            response = await self.model_client.chat.completions.create(
                model=self.extraction_model,
                messages=messages,
                **options,
            )
        usage = getattr(response, "usage", None)
        tokens = int(getattr(usage, "total_tokens", 0) or 0)
        return response.choices[0].message.content or "", tokens

    def _require_model(self) -> None:
        if self.model_client is None or not self.extraction_model:
            raise RuntimeError("LLM access is not configured")

    @staticmethod
    def _clamp_temperature(value: Any) -> float:
        return min(max(float(value), 0.0), 2.0)

    @staticmethod
    def _clamp_max_tokens(value: Any) -> int | None:
        if value is None:
            return None
        return min(max(int(value), 1), 32_000)

    async def _complete(self, state: BrokerSession, params: dict[str, Any]) -> str:
        self._require_model()
        prompt = str(params.get("prompt", "")).strip()
        if not prompt:
            raise ValueError("prompt must not be empty")
        await state.policy.consume_llm(1)
        system = params.get("system")
        answer, tokens = await self._chat(
            prompt,
            system=str(system) if system else None,
            temperature=self._clamp_temperature(params.get("temperature", 0.2)),
            max_tokens=self._clamp_max_tokens(params.get("max_tokens")),
        )
        await state.policy.record_pipeline_model_tokens(tokens)
        _EVENT_MODEL_TOKENS.set(_EVENT_MODEL_TOKENS.get() + tokens)
        return answer

    async def _complete_many(self, state: BrokerSession, params: dict[str, Any]) -> list[str]:
        self._require_model()
        prompts = [str(prompt) for prompt in params.get("prompts", [])]
        if not prompts:
            return []
        if any(not prompt.strip() for prompt in prompts):
            raise ValueError("prompts must not contain empty strings")
        # Charge the whole fan-out up front: a partially charged batch that then
        # trips the quota mid-flight would leave the caller unable to tell which
        # prompts actually ran.
        await state.policy.consume_llm(len(prompts))
        system = params.get("system")
        temperature = self._clamp_temperature(params.get("temperature", 0.2))
        max_tokens = self._clamp_max_tokens(params.get("max_tokens"))
        concurrency = min(max(int(params.get("concurrency", 4)), 1), 12)
        gate = asyncio.Semaphore(concurrency)

        async def one(prompt: str) -> tuple[str, int]:
            async with gate:
                return await self._chat(
                    prompt,
                    system=str(system) if system else None,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )

        results = await asyncio.gather(*(one(prompt) for prompt in prompts))
        total_tokens = sum(tokens for _, tokens in results)
        await state.policy.record_pipeline_model_tokens(total_tokens)
        _EVENT_MODEL_TOKENS.set(_EVENT_MODEL_TOKENS.get() + total_tokens)
        return [answer for answer, _ in results]

    async def _extract_many(
        self,
        state: BrokerSession,
        params: dict[str, Any],
    ) -> list[dict[str, Any]]:
        self._require_model()
        items = params.get("items", [])
        await state.policy.consume_llm(len(items))
        schema = params.get("schema", {})
        instruction = str(params.get("instruction", ""))
        concurrency = min(max(int(params.get("concurrency", 4)), 1), 12)
        gate = asyncio.Semaphore(concurrency)

        async def extract(item: Any) -> tuple[str, int]:
            prompt = (
                f"{instruction}\n\nJSON schema:\n{json.dumps(schema)}\n\n"
                f"Input:\n{json.dumps(item, ensure_ascii=False, default=str)}\n\n"
                "Return only one JSON object."
            )
            async with gate:
                content, tokens = await self._chat(prompt, json_object=True)
            return content, tokens

        results = await asyncio.gather(*(extract(item) for item in items))
        total_tokens = sum(tokens for _, tokens in results)
        await state.policy.record_pipeline_model_tokens(total_tokens)
        _EVENT_MODEL_TOKENS.set(_EVENT_MODEL_TOKENS.get() + total_tokens)
        return [json.loads(content or "{}") for content, _ in results]
