from __future__ import annotations

import json
from pathlib import Path

from opensac_sdk.models import ContentSnippet, SearchHit

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
                ref="",
                backend="web",
                title=f"Frozen evidence page {index}",
                url=f"https://example.test/frozen/{index}",
                snippet="frozen component fixture",
                rank=index + 1,
            )
            for index in range(offset, min(offset + limit, len(self.documents)))
        ]

    async def content(self, hits, *, query=None):
        del query
        return [
            ContentSnippet(
                ref=hit.ref,
                title=hit.title,
                url=hit.url,
                text=self.documents[int(str(hit.url).rsplit("/", 1)[-1])],
            )
            for hit in hits
        ]


def _session() -> Session:
    return Session(
        id="sess-passage-quality",
        token="token",
        backends=["web"],
        workspace="/tmp/session-passage-quality",
        budget=ResourceBudget(),
    )


def _reciprocal_rank(texts: list[str], gold_span: str) -> float:
    return next(
        (1.0 / rank for rank, text in enumerate(texts, start=1) if gold_span in text),
        0.0,
    )


async def test_frozen_gold_spans_improve_recall_at_5_and_mrr_over_snippets() -> None:
    fixture = json.loads(
        (Path(__file__).parent / "data" / "passage_retrieval_gold.json").read_text(encoding="utf-8")
    )
    baseline_reciprocal_ranks: list[float] = []
    passage_reciprocal_ranks: list[float] = []
    resolvable = 0
    returned = 0

    for case in fixture["cases"]:
        document = "\n\n".join(section["text"] * section["repeat"] for section in case["sections"])
        service = BrokerService({"web": FrozenPageBackend([document])})
        service.register_session(_session())
        ref = (await service.call("token", "search.query", {"query": case["query"]}))[0]["ref"]

        baseline = await service.call(
            "token",
            "content.snippets",
            {
                "query": case["query"],
                "refs": [ref],
                "max_tokens": 60,
                "max_tokens_per_page": 60,
            },
        )
        report = await service.call(
            "token",
            "content.passages",
            {
                "query": case["query"],
                "refs": [ref],
                "limit": 5,
                "max_per_ref": 5,
            },
        )

        baseline_reciprocal_ranks.append(
            _reciprocal_rank([row["text"] for row in baseline], case["gold_span"])
        )
        passage_reciprocal_ranks.append(
            _reciprocal_rank(
                [row["text"] for row in report["passages"]],
                case["gold_span"],
            )
        )
        requests = [
            {"ref": row["ref"], "locator": row["locator"]}
            for row in report["passages"]
            if row.get("locator") is not None
        ]
        resolved = await service.call(
            "token",
            "citations.resolve",
            {"requests": requests},
        )
        returned += len(report["passages"])
        resolvable += sum(
            item["evidence"] == row["text"]
            for item, row in zip(resolved, report["passages"], strict=True)
        )

    baseline_recall_at_5 = sum(rank > 0 for rank in baseline_reciprocal_ranks) / len(
        baseline_reciprocal_ranks
    )
    passage_recall_at_5 = sum(rank > 0 for rank in passage_reciprocal_ranks) / len(
        passage_reciprocal_ranks
    )
    baseline_mrr = sum(baseline_reciprocal_ranks) / len(baseline_reciprocal_ranks)
    passage_mrr = sum(passage_reciprocal_ranks) / len(passage_reciprocal_ranks)

    assert passage_recall_at_5 > baseline_recall_at_5
    assert passage_mrr > baseline_mrr
    assert returned > 0
    assert resolvable / returned == 1.0
