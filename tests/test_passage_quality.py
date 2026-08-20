from __future__ import annotations

import json
from pathlib import Path

from opensac._contracts import ContentSnippet, SearchHit
from opensac.broker.service import BrokerService
from opensac.models import ResourceBudget, Session


class FrozenPageBackend:
    name = "web"
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
        return ContentSnippet(
            source=hit.source,
            title=hit.title,
            url=hit.url,
            text=self.documents[int(str(hit.url).rsplit("/", 1)[-1])],
        )


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
        service = BrokerService({"web": FrozenPageBackend([document])})
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
                "max_per_source": 5,
            },
        )

        assert any(case["gold_span"] in row["text"] for row in report["passages"])
        assert all("locator" not in row for row in report["passages"])
