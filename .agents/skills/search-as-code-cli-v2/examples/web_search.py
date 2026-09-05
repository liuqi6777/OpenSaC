"""Basic search and bounded candidate preview; send this body to agent-run on stdin.

Adapt QUERY to the task. Snippets select candidates; they are not verified evidence.
"""

from opensac_sdk import sdk

QUERY = "Python 3.13 free threading"
hits = sdk.search(QUERY, limit=5)
if hits is None:
    observation = {"status": "failed", "query": QUERY}
else:
    observation = {
        "status": "ok",
        "total": len(hits),
        "candidates": [
            {
                "source": hit["source"],
                "title": hit["title"][:120],
                "snippet": hit["snippet"][:160],
            }
            for hit in hits[:5]
        ],
    }
if hits is None:
    print(f"Search failed: {QUERY}")
else:
    output = [f"{len(hits)} candidates"]
    for candidate in observation["candidates"]:
        output.append(f"{candidate['title']} — {candidate['source']}\n{candidate['snippet']}")
    print("\n".join(output))
