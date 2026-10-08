"""Collect source-linked passages for review in a new output directory.

Usage: python evidence.py QUERY --pattern REGEX --output DIRECTORY
Requires configured search and fetch providers. Adapt candidate selection to the task.
Exit codes: 0 = requests succeeded (evidence may still be missing), 1 = SDK failure,
2 = invalid CLI input. This is one retrieval pass, not an automatic research loop.
"""

import argparse
import json
import re
import sys
from itertools import islice
from pathlib import Path
from typing import Any

from opensac import dedup, sdk
from opensac.errors import OpenSACError


def save_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def collect_passages(query: str, pattern: re.Pattern[str], output: Path) -> int:
    # Require a new directory so a fresh retrieval cannot overwrite saved evidence.
    output.mkdir(parents=True, exist_ok=False)
    hits = sdk.search(query, limit=5)
    save_json(
        output / "search.json",
        {"query": query, "hits": [hit.model_dump(mode="json") for hit in hits]},
    )
    # A small starting selection; review candidate authority and relevance before adapting it.
    urls = [hit.url for hit in dedup(hits)[:3]]
    findings: list[dict[str, Any]] = []
    failed = False
    results = sdk.content.fetch_many(urls) if urls else []
    for index, (url, result) in enumerate(zip(urls, results, strict=True)):
        record: dict[str, Any] = {"input_url": url, "passages": []}
        if not result.ok:
            failed = True
            record.update(status="fetch_failed", error=result.error.model_dump(mode="json"))
        else:
            document = result.data
            document_path = output / f"document-{index}.json"
            document_path.write_text(document.model_dump_json(), encoding="utf-8")
            passages = []
            for match in islice(pattern.finditer(document.text), 2):
                start = max(0, match.start() - 250)
                end = min(len(document.text), match.end() + 250)
                passages.append({"start": start, "end": end, "text": document.text[start:end]})
            status = "needs_review" if passages else "no_match"
            if not document.text:
                status = "empty_text"
            record.update(
                url=document.url,
                document=document_path.name,
                passages=passages,
                status=status,
            )
        findings.append(record)

    evidence_path = output / "evidence.json"
    save_json(evidence_path, {"query": query, "pattern": pattern.pattern, "sources": findings})
    print(f"Saved candidates and full documents: {output}")
    print(f"Passages and retrieval gaps: {evidence_path}")
    if not urls:
        print("No candidates; reformulate the query or use a known source.")
    for record in findings:
        print(record["status"], record.get("url", record["input_url"]))
        if record["status"] == "fetch_failed":
            print(record["error"]["code"])
        for passage in record["passages"]:
            print(passage["text"])
    print("Review passages and unresolved gaps before answering; matches do not verify a claim.")
    return 1 if failed else 0


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("query")
parser.add_argument("--pattern", required=True, help="Case-insensitive local search regex")
parser.add_argument("--output", required=True, type=Path, help="New task-specific directory")
args = parser.parse_args()
try:
    pattern = re.compile(args.pattern, re.IGNORECASE)
except re.error as error:
    parser.error(f"Invalid pattern: {error}")
try:
    exit_code = collect_passages(args.query, pattern, args.output)
except OpenSACError as error:
    print(f"Research request failed: {error.code}: {error.message}", file=sys.stderr)
    exit_code = 1
finally:
    sdk.close()
sys.exit(exit_code)
