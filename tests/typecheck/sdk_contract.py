from __future__ import annotations

from typing import Any

from opensac_sdk import Outcome, sdk

search_outcome = sdk.search("OpenSAC", limit=3)
public_outcome_annotation: Outcome[list[Any]] = search_outcome
if search_outcome.status == "failure":
    search_failure_code: str = search_outcome.error.code
    hits = []
else:
    hits = search_outcome.value
source: str = hits[0].source
rank: int = hits[0]["rank"]

batches = sdk.search.many(["OpenSAC", "search as code"])
search_status: str = batches[0].status
search_hit_count: int = len(batches[0].value) if batches[0].status == "success" else 0
search_error_code: str | None = batches[0].error.code if batches[0].status == "failure" else None

fused = sdk.search.fuse_rrf(batches)
fused_rank: int | None = fused[0].rank if fused else None

diagnostic = sdk.search("provider diagnostics")
if diagnostic.status == "failure":
    broker_provider: str | None = diagnostic.error.provider
    broker_component: str | None = diagnostic.error.component
    broker_scope: str | None = diagnostic.error.scope
else:
    broker_provider = broker_component = broker_scope = None

read_outcome = sdk.content.read(source, start_line=1, line_count=20)
if read_outcome.status == "failure":
    raise RuntimeError(read_outcome.error.message)
row = read_outcome.value
text: str = row.text
next_line: int | None = row.window.next.start_line if row.window.next else None
fetch_outcome = sdk.content.fetch(source)
if fetch_outcome.status == "failure":
    raise RuntimeError(fetch_outcome.error.message)
document = fetch_outcome.value
document_title: str = document.title
fetch_outcomes = sdk.content.fetch_many([source], concurrency=2)
fetch_status: str = fetch_outcomes[0].status
fetched_document = fetch_outcomes[0].value
fetched_document_title: str | None = (
    fetched_document.title if fetched_document is not None else None
)
fetch_error_code: str | None = (
    fetch_outcomes[0].error.code if fetch_outcomes[0].status == "failure" else None
)
input_index: int = fused[0].provenance[0].input_index if fused else 0

grep_outcomes = sdk.content.grep("OpenSAC", sources=[source], mode="literal", case_sensitive=True)
grep_status: str = grep_outcomes[0].status
match_line: int = (
    grep_outcomes[0].value.matches[0].line if grep_outcomes[0].status == "success" else 0
)
grep_exhaustive: bool = (
    grep_outcomes[0].value.next_start_line is None
    if grep_outcomes[0].status == "success"
    else False
)

capabilities_outcome = sdk.capabilities()
if capabilities_outcome.status == "failure":
    raise RuntimeError(capabilities_outcome.error.message)
capabilities = capabilities_outcome.value
capability_contract: int = capabilities.contracts.capability
sdk.workspace.write_json("checkpoint.json", {"source": source})
workspace_files: list[str] = sdk.workspace.list()
checkpoint_source: object = sdk.workspace.read_json("checkpoint.json")["source"]

extraction_outcome = sdk.llm.extract(
    {"text": text},
    instruction="Extract a label.",
    schema={
        "type": "object",
        "properties": {"label": {"type": "string"}},
        "required": ["label"],
        "additionalProperties": False,
    },
)
if extraction_outcome.status == "failure":
    raise RuntimeError(extraction_outcome.error.message)
extraction = extraction_outcome.value
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
extracted_data = extract_outcomes[0].value
many_extracted_label: object | None = (
    extracted_data["label"] if extracted_data is not None else None
)
extract_error_code: str | None = (
    extract_outcomes[0].error.code if extract_outcomes[0].status == "failure" else None
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
