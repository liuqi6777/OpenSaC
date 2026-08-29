from __future__ import annotations

from opensac_sdk import BrokerError, sdk

hits = sdk.search("OpenSAC", limit=3)
source: str = hits[0].source
rank: int = hits[0]["rank"]

batches = sdk.search.many(["OpenSAC", "search as code"])
search_status: str = batches[0].status
search_hit_count: int = len(batches[0].hits)
search_error = batches[0].error
search_error_code: str | None = search_error.code if search_error is not None else None

fused = sdk.search.fuse_rrf(batches)
fused_rank: int | None = fused[0].rank if fused else None

try:
    sdk.search("provider diagnostics")
except BrokerError as error:
    broker_provider: str | None = error.provider
    broker_component: str | None = error.component
    broker_scope: str | None = error.scope
else:
    broker_provider = broker_component = broker_scope = None

row = sdk.content.read(source, start_line=1, line_count=20)
text: str = row.text
next_line: int | None = row.window.next.start_line if row.window.next else None
document = sdk.content.fetch(source)
document_title: str = document.title
fetch_outcomes = sdk.content.fetch_many([source], concurrency=2)
fetch_status: str = fetch_outcomes[0].status
fetched_document = fetch_outcomes[0].document
fetched_document_title: str | None = (
    fetched_document.title if fetched_document is not None else None
)
fetch_error = fetch_outcomes[0].error
fetch_error_code: str | None = fetch_error.code if fetch_error is not None else None
input_index: int = fused[0].provenance[0].input_index if fused else 0

grep_outcomes = sdk.content.grep("OpenSAC", sources=[source], mode="literal", case_sensitive=True)
grep_status: str = grep_outcomes[0].status
match_line: int = grep_outcomes[0].matches[0].line
grep_exhaustive: bool = grep_outcomes[0].next_start_line is None

capabilities = sdk.capabilities()
capability_contract: int = capabilities.contracts.capability
sdk.workspace.write_json("checkpoint.json", {"source": source})
workspace_files: list[str] = sdk.workspace.list()
checkpoint_source: object = sdk.workspace.read_json("checkpoint.json")["source"]

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
extracted_label: object = extraction["label"]
extract_outcomes = sdk.llm.extract_many(
    [{"text": text}],
    instruction="Extract a label.",
    schema={
        "type": "object",
        "properties": {"label": {"type": "string"}},
        "required": ["label"],
    },
)
extract_status: str = extract_outcomes[0].status
extracted_data = extract_outcomes[0].data
many_extracted_label: object | None = (
    extracted_data["label"] if extracted_data is not None else None
)
extract_error = extract_outcomes[0].error
extract_error_code: str | None = extract_error.code if extract_error is not None else None

print(
    {
        "source": source,
        "rank": rank,
        "fused_rank": fused_rank,
        "input_index": input_index,
        "match_line": match_line,
        "grep_exhaustive": grep_exhaustive,
        "capability_contract": capability_contract,
        "workspace_files": workspace_files,
        "checkpoint_source": checkpoint_source,
        "document_title": document_title,
        "fetch_status": fetch_status,
        "fetched_document_title": fetched_document_title,
        "fetch_error_code": fetch_error_code,
        "extracted_label": extracted_label,
        "extract_status": extract_status,
        "many_extracted_label": many_extracted_label,
        "extract_error_code": extract_error_code,
        "search_status": search_status,
        "search_hit_count": search_hit_count,
        "search_error_code": search_error_code,
        "grep_status": grep_status,
        "broker_provider": broker_provider,
        "broker_component": broker_component,
        "broker_scope": broker_scope,
        "next_line": next_line,
    }
)
