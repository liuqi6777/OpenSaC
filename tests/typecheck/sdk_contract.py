from __future__ import annotations

from opensac_sdk import BrokerError, sdk

hits = sdk.search("OpenSAC", limit=3)
source: str = hits[0].source
rank: int = hits[0]["rank"]

batches = sdk.search.many(["OpenSAC", "search as code"])
search_status: str = batches[0].status
search_hit_count: int = len(batches[0].hits)

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
input_index: int = fused[0].provenance[0].input_index if fused else 0

grep_outcomes = sdk.content.grep("OpenSAC", sources=[source], mode="literal", case_sensitive=True)
grep_status: str = grep_outcomes[0].status
match_line: int = grep_outcomes[0].matches[0].line
grep_exhaustive: bool = grep_outcomes[0].next_start_line is None

capabilities = sdk.session.capabilities()
capability_contract: int = capabilities.contracts.capability
usage = sdk.session.usage()
content_fetches: int = usage.content_fetches

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

sdk.output.submit(
    {
        "rank": rank,
        "fused_rank": fused_rank,
        "input_index": input_index,
        "match_line": match_line,
        "grep_exhaustive": grep_exhaustive,
        "capability_contract": capability_contract,
        "content_fetches": content_fetches,
        "document_title": document_title,
        "extracted_label": extracted_label,
        "search_status": search_status,
        "search_hit_count": search_hit_count,
        "grep_status": grep_status,
        "broker_provider": broker_provider,
        "broker_component": broker_component,
        "broker_scope": broker_scope,
        "next_line": next_line,
    },
    citations=[source],
)
