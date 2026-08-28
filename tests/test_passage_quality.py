from __future__ import annotations

import json
from pathlib import Path

from opensac.backends.document import DocumentContent, DocumentHandle
from opensac.backends.search import SearchHit
from opensac.broker.service import BrokerService
from opensac.models import ResourceBudget, Session


def _broker_service(search_backends, *, document_backends=None, **kwargs):
    if document_backends is None:
        document_backends = search_backends
    return BrokerService(
        search_backends,
        document_backends=document_backends,
        **kwargs,
    )


class FrozenPageBackend:
    name = "web"
    source_kind = "public_url"
    supports_domains = True
    max_depth = 100

    def __init__(self, documents: list[str]) -> None:
        self.documents = documents

    async def search(self, query, *, limit, offset=0, domains=None):
        del query, domains
        return [
            SearchHit(
                source="",
                backend="web",
                title=f"Frozen evidence page {index}",
                url=f"https://example.test/frozen/{index}",
                snippet="frozen component fixture",
                rank=index + 1,
            )
            for index in range(offset, min(offset + limit, len(self.documents)))
        ]

    async def fetch(self, hit, *, query=None):
        del query
        return DocumentContent(
            source=hit.source,
            title=hit.title,
            url=hit.url,
            text=self.documents[int(str(hit.url).rsplit("/", 1)[-1])],
        )

    @staticmethod
    def fetch_candidates(hit: DocumentHandle) -> list[DocumentHandle]:
        return [hit]


def _session() -> Session:
    return Session(
        id="sess-passage-quality",
        token="token",
        backends=["web"],
        workspace="/tmp/session-passage-quality",
        budget=ResourceBudget(),
    )


async def test_frozen_gold_passages_are_retrieved_and_resolvable() -> None:
    fixture = json.loads(
        (Path(__file__).parent / "data" / "passage_retrieval_gold.json").read_text(encoding="utf-8")
    )

    for case in fixture["cases"]:
        document = "\n\n".join(section["text"] * section["repeat"] for section in case["sections"])
        service = _broker_service({"web": FrozenPageBackend([document])})
        service.register_session(_session())
        source = (await service.call("token", "search.query", {"query": case["query"]}))[0][
            "source"
        ]

        report = await service.call(
            "token",
            "content.passages",
            {
                "query": case["query"],
                "sources": [source],
                "limit": 5,
                "limit_per_source": 5,
            },
        )

        assert any(case["gold_span"] in row["text"] for row in report["passages"])
        assert all("locator" not in row for row in report["passages"])
