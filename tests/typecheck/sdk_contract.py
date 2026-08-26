from __future__ import annotations

from opensac_sdk import BrokerError, sdk

hits = sdk.search("OpenSAC", limit=3)
source: str = hits[0].source
rank: int = hits[0]["rank"]

batches = sdk.search.many(["OpenSAC", "search as code"])
if batches.failures:
    failure_provider: str | None = batches.failures[0].provider
    failure_component: str | None = batches.failures[0].component
    failure_scope: str | None = batches.failures[0].scope
else:
    failure_provider = failure_component = failure_scope = None

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

row = sdk.content.read(source, offset=1, limit=20)
text: str = row.text
next_offset: int | None = row.metadata.next_offset

rows = sdk.content.read_many(
    [
        {"source": source, "offset": 1, "limit": 20},
        {"source": source, "offset": 21, "limit": 20, "max_chars": 8_000},
    ]
)
input_index: int = rows.results[0].input_index

report = sdk.content.grep([source], "OpenSAC", mode="literal", case_sensitive=True)
match_line: int = report.matches[0].line
scan_complete: bool = report.source_results[0].scan_complete

capabilities = sdk.session.capabilities()
capability_contract: int = capabilities.contracts.capability
usage = sdk.session.usage()
content_backend_fetches: int = usage.content_backend_fetches

extractions = sdk.llm.extract_many(
    [{"text": text}],
    instruction="Extract a label.",
    schema={
        "type": "object",
        "properties": {"label": {"type": "string"}},
        "required": ["label"],
        "additionalProperties": False,
    },
)
failure_code: str | None = extractions.failures[0].code if extractions.failures else None

sdk.output.submit(
    {
        "rank": rank,
        "fused_rank": fused_rank,
        "input_index": input_index,
        "match_line": match_line,
        "scan_complete": scan_complete,
        "capability_contract": capability_contract,
        "content_backend_fetches": content_backend_fetches,
        "failure_code": failure_code,
        "failure_provider": failure_provider,
        "failure_component": failure_component,
        "failure_scope": failure_scope,
        "broker_provider": broker_provider,
        "broker_component": broker_component,
        "broker_scope": broker_scope,
        "next_offset": next_offset,
    },
    citations=[source],
)
