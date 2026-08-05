from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from openai import AsyncOpenAI
from opensac_sdk.models import ContentSnippet, SearchBatch, SearchHit

from opensac.backends.base import SearchBackend
from opensac.broker.policy import CapabilityPolicy
from opensac.models import Session


@dataclass
class BrokerSession:
    session: Session
    policy: CapabilityPolicy
    references: dict[str, SearchHit] = field(default_factory=dict)


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

    async def call(self, token: str, method: str, params: dict[str, Any]) -> Any:
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
            "llm.extract_many": self._extract_many,
        }
        handler = handlers.get(method)
        if handler is None:
            raise ValueError(f"Unsupported capability: {method}")
        return await handler(state, params)

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
        rows = await self._fetch_content(state, hits, query=str(params.get("query", "")))
        per_page_chars = max(int(params.get("max_tokens_per_page", 1000)), 1) * 4
        total_chars = max(int(params.get("max_tokens", 4000)), 1) * 4
        used = 0
        for row in rows:
            row["text"] = row["text"][:per_page_chars]
            if used + len(row["text"]) > total_chars:
                row["text"] = row["text"][: max(total_chars - used, 0)]
            used += len(row["text"])
        return [row for row in rows if row["text"]]

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

        async def fetch(name: str, backend_hits: list[SearchHit]) -> list[ContentSnippet]:
            backend = self.backends.get(name)
            if backend is None:
                raise RuntimeError(f"Backend '{name}' is not configured")
            async with self._semaphore:
                return await backend.content(backend_hits, query=query)

        chunks = await asyncio.gather(*(fetch(name, rows) for name, rows in grouped.items()))
        return [item.model_dump(mode="json") for chunk in chunks for item in chunk]

    async def _extract_many(
        self,
        state: BrokerSession,
        params: dict[str, Any],
    ) -> list[dict[str, Any]]:
        if self.model_client is None or not self.extraction_model:
            raise RuntimeError("LLM extraction is not configured")
        items = params.get("items", [])
        await state.policy.consume_llm(len(items))
        schema = params.get("schema", {})
        instruction = str(params.get("instruction", ""))
        concurrency = min(max(int(params.get("concurrency", 4)), 1), 12)
        gate = asyncio.Semaphore(concurrency)

        async def extract(item: Any) -> dict[str, Any]:
            prompt = (
                f"{instruction}\n\nJSON schema:\n{json.dumps(schema)}\n\n"
                f"Input:\n{json.dumps(item, ensure_ascii=False, default=str)}\n\n"
                "Return only one JSON object."
            )
            async with gate, self._semaphore:
                response = await self.model_client.chat.completions.create(
                    model=self.extraction_model,
                    messages=[{"role": "user", "content": prompt}],
                    response_format={"type": "json_object"},
                )
            content = response.choices[0].message.content or "{}"
            return json.loads(content)

        return await asyncio.gather(*(extract(item) for item in items))
