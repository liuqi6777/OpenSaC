"""Save research artifacts in this Python environment; resume by reading the JSON files."""

from pathlib import Path

from opensac import sdk

output = Path("research-output")
output.mkdir(exist_ok=True)
hits = sdk.search("Search as Code", limit=5)
(output / "hits.jsonl").write_text(
    "\n".join(hit.model_dump_json() for hit in hits), encoding="utf-8"
)
if hits:
    documents = sdk.content.fetch_many([hit.url for hit in hits])
    for index, item in enumerate(documents):
        if item.error is not None:
            print(f"Failed {hits[index].url}: {item.error.code}")
            continue
        assert item.data is not None
        (output / f"document-{index}.json").write_text(
            item.data.model_dump_json(), encoding="utf-8"
        )
        print(f"Saved {item.data.url}")
