"""Search, locally fuse, rerank and fetch. Requires search/fetch/rerank configuration."""

from opensac import Client, fuse


def main() -> None:
    query = "retrieval evaluation"
    with Client() as sac:
        batches = sac.search.many([query, "evaluating retrieval quality"], limit=10)
        lists = []
        for batch in batches:
            if batch.error is not None:
                print("Search failed:", batch.error.code)
            else:
                assert batch.data is not None
                lists.append(batch.data)
        pool = fuse(lists)
        if not pool:
            print("No candidates available.")
            return
        best = sac.rerank(query, pool[:100], top_n=min(5, len(pool)))
        for hit, result in zip(
            best, sac.content.fetch_many([hit.url for hit in best]), strict=True
        ):
            if result.error is not None:
                print(hit.url, result.error.code)
            else:
                assert result.data is not None
                print(result.data.url, result.data.text[:500])


if __name__ == "__main__":
    main()
