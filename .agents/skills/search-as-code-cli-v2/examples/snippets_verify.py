"""Search, fetch, and locate an excerpt with Python; send this body to agent-run on stdin.

Adapt QUERY and PATTERN. A match locates evidence; inspect its context before making a claim.
"""

import re

from opensac_sdk import sdk

QUERY = "Python 3.13 free threading"
PATTERN = "free.thread"
hits = sdk.search(QUERY, limit=5)
if hits is None:
    print("Search failed; inspect the OpenSAC warning.")
else:
    # Inspect two distinct candidates; choose sources for the actual question.
    sources = list(dict.fromkeys(hit["source"] for hit in hits))[:2]
    documents = sdk.content.fetch_many(sources) if sources else []
    output = []
    for source, document in zip(sources, documents, strict=True):
        output.append(source)
        if document is None:
            output.append("Fetch failed.")
            continue
        text = document["text"]
        match = re.search(PATTERN, text, re.IGNORECASE)
        if match:
            start, end = max(0, match.start() - 120), match.end() + 280
            output.append(text[start:end])
        else:
            output.append("No match for this pattern in the fetched text.")
    print("\n".join(output) if sources else "No search results.")
