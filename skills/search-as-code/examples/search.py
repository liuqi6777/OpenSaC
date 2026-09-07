"""Search for candidates: python search.py [query ...].

Requires Python 3.12+ and opensac in the active environment.
Exit codes: 0 = successful search (including no hits), 1 = SDK error.
"""

import sys

from opensac import sdk
from opensac.errors import OpenSACError

query = " ".join(sys.argv[1:]) or "Python 3.13 free threading"
try:
    hits = sdk.search(query, limit=5)
    print(f"{len(hits)} candidates")
    for hit in hits:
        print(f"{hit.title}\n{hit.url}\n{hit.snippet[:300]}")
except OpenSACError as error:
    print(f"Search failed: {error.code}: {error.message}", file=sys.stderr)
    sys.exit(1)
finally:
    sdk.close()
