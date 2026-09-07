"""Fetch known URLs: python fetch.py URL [URL ...].

Requires Python 3.12+ and opensac in the active environment.
Prints previews, not full documents. Exit codes: 0 = all URLs succeeded,
1 = at least one fetch failed, 2 = no URLs supplied.
"""

import sys

from opensac import sdk
from opensac.errors import OpenSACError

urls = sys.argv[1:]
if not urls:
    print("Usage: python fetch.py URL [URL ...]", file=sys.stderr)
    sys.exit(2)

failed = False
try:
    for start in range(0, len(urls), 32):
        chunk = urls[start : start + 32]
        results = sdk.content.fetch_many(chunk)
        for url, result in zip(chunk, results, strict=True):
            if result.ok:
                print(result.data.url, result.data.text[:1500], sep="\n")
            else:
                failed = True
                print(f"Fetch failed: {url}: {result.error.code}", file=sys.stderr)
except OpenSACError as error:
    failed = True
    print(f"Fetch failed: {error.code}: {error.message}", file=sys.stderr)
finally:
    sdk.close()
sys.exit(1 if failed else 0)
