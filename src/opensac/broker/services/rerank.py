from __future__ import annotations

import hashlib
from dataclasses import dataclass

from pydantic import TypeAdapter

from opensac.backends.rerank.base import RerankScore, TextReranker
from opensac.broker.providers.execution import ProviderExecutor
from opensac.broker.session import BrokerSession
from opensac.provider import ProviderRequestError, ProviderRuntime

from .base import ServiceExecution

_RERANK_SCORES = TypeAdapter(list[RerankScore])


@dataclass(frozen=True, slots=True)
class RerankItem:
    """Caller-owned identity paired with provider-visible ranking text."""

    id: str
    text: str


class RerankService(ServiceExecution):
    """Shared text reranking service reusable by search and content orchestration."""

    component = "rerank"

    def __init__(
        self,
        backend: TextReranker,
        providers: ProviderExecutor,
        runtime: ProviderRuntime,
    ) -> None:
        super().__init__(backend, providers, runtime)

    @property
    def name(self) -> str:
        return self.backend.name

    async def score(
        self,
        state: BrokerSession,
        query: str,
        items: list[RerankItem],
    ) -> dict[str, float]:
        ids = [item.id for item in items]
        if len(set(ids)) != len(ids):
            raise ValueError("rerank item ids must be unique")
        if not items:
            return {}

        async def request() -> list[float]:
            results = _RERANK_SCORES.validate_python(
                await self.backend.rerank(query, [item.text for item in items]),
                strict=True,
            )
            scores: dict[int, float] = {}
            for result in results:
                if result.index >= len(items) or result.index in scores:
                    raise self._invalid_response("Reranker returned invalid indexed scores.")
                scores[result.index] = result.score
            if set(scores) != set(range(len(items))):
                raise self._invalid_response("Reranker returned an incomplete score set.")
            return [scores[index] for index in range(len(items))]

        scores = await self.run(
            state,
            request_indexes=list(range(len(items))),
            request_value={
                "ranker": self.backend.name,
                "query": query,
                "items": [hashlib.sha256(item.text.encode("utf-8")).hexdigest() for item in items],
            },
            request=request,
            preflight=self.backend.preflight,
        )
        return {item.id: scores[index] for index, item in enumerate(items)}

    @staticmethod
    def _invalid_response(message: str) -> ProviderRequestError:
        return ProviderRequestError(
            "provider_invalid_response",
            message,
            retryable=False,
        )
