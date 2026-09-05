from __future__ import annotations

from opensac_sdk import sdk

hits = sdk.search("OpenSAC", limit=3)
if hits is None:
    hits = []
source: str = hits[0].source
rank: int = hits[0]["rank"]

queries = ["OpenSAC", "search as code"]
batches = sdk.search.many(queries)
first_batch = batches[0]
search_hit_count: int = len(first_batch) if first_batch is not None else 0

fused = sdk.search.fuse_rrf(queries, batches)
fused_rank: int | None = fused[0].rank if fused else None
input_index: int = fused[0].provenance[0].input_index if fused else 0

row = sdk.content.read(source, start_line=1, line_count=20)
if row is None:
    raise RuntimeError("content unavailable")
text: str = row.text
next_line: int | None = row.window.next.start_line if row.window.next else None

document = sdk.content.fetch(source)
if document is None:
    raise RuntimeError("document unavailable")
document_title: str = document.title

fetched_documents = sdk.content.fetch_many([source], concurrency=2)
fetched_document = fetched_documents[0]
fetched_document_title: str | None = (
    fetched_document.title if fetched_document is not None else None
)

grep_results = sdk.content.grep(
    "OpenSAC",
    sources=[source],
    mode="literal",
    case_sensitive=True,
)
grep_result = grep_results[0]
match_line: int = grep_result.matches[0].line if grep_result is not None else 0
grep_exhaustive: bool = grep_result.next_start_line is None if grep_result is not None else False

capabilities = sdk.capabilities()
if capabilities is None:
    raise RuntimeError("capabilities unavailable")
capability_contract: int = capabilities.contracts.capability


extraction = sdk.llm.extract(
    {"text": text},
    instruction="Extract a label.",
    schema={
        "type": "object",
        "properties": {"label": {"type": "string"}},
        "required": ["label"],
        "additionalProperties": False,
    },
)
if extraction is None:
    raise RuntimeError("extraction unavailable")
extracted_label: object = extraction["label"]

extractions = sdk.llm.extract_many(
    [{"text": text}],
    instruction="Extract a label.",
    schema={
        "type": "object",
        "properties": {"label": {"type": "string"}},
        "required": ["label"],
    },
)
extracted_data = extractions[0]
many_extracted_label: object | None = (
    extracted_data["label"] if extracted_data is not None else None
)

print(
    {
        "source": source,
        "rank": rank,
        "fused_rank": fused_rank,
        "input_index": input_index,
        "match_line": match_line,
        "grep_exhaustive": grep_exhaustive,
        "capability_contract": capability_contract,
        "document_title": document_title,
        "fetched_document_title": fetched_document_title,
        "extracted_label": extracted_label,
        "many_extracted_label": many_extracted_label,
        "search_hit_count": search_hit_count,
        "next_line": next_line,
    }
)
