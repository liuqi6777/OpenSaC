from collections.abc import Mapping
from typing import Any, Literal, Protocol, Required, TypedDict, overload

class BrokerError(RuntimeError):
    code: str
    retryable: bool
    attempts: int | None
    provider_status: int | None
    retry_after_seconds: float | None
    provider: str | None
    operation: str | None
    scope: Literal["request", "resource", "provider", "unknown"] | None

class _OperationFailureRecord(Protocol):
    code: str
    message: str
    retryable: bool
    @overload
    def __getitem__(self, key: Literal["code", "message"]) -> str: ...
    @overload
    def __getitem__(self, key: Literal["retryable"]) -> bool: ...

class _FailureRecord(_OperationFailureRecord, Protocol):
    attempts: int
    provider_status: int | None
    retry_after_seconds: float | None
    provider: str | None
    operation: str | None
    scope: Literal["request", "resource", "provider", "unknown"] | None
    @overload
    def __getitem__(self, key: Literal["attempts"]) -> int: ...
    @overload
    def __getitem__(
        self,
        key: Literal["provider_status"],
    ) -> int | None: ...
    @overload
    def __getitem__(
        self,
        key: Literal["retry_after_seconds"],
    ) -> float | None: ...
    @overload
    def __getitem__(self, key: Literal["provider", "operation"]) -> str | None: ...
    @overload
    def __getitem__(
        self,
        key: Literal["scope"],
    ) -> Literal["request", "resource", "provider", "unknown"] | None: ...

class _SearchHitRecord(Protocol):
    source: str
    backend: str
    title: str
    snippet: str
    date: str | None
    score: float | None
    rank: int
    metadata: Mapping[str, Any]
    @overload
    def __getitem__(
        self,
        key: Literal["source", "backend", "title", "snippet"],
    ) -> str: ...
    @overload
    def __getitem__(self, key: Literal["date"]) -> str | None: ...
    @overload
    def __getitem__(self, key: Literal["score"]) -> float | None: ...
    @overload
    def __getitem__(self, key: Literal["rank"]) -> int: ...
    @overload
    def __getitem__(self, key: Literal["metadata"]) -> Mapping[str, Any]: ...

class _SearchBatchRecord(Protocol):
    query: str
    hits: list[_SearchHitRecord]
    failure: _FailureRecord | None
    @overload
    def __getitem__(self, key: Literal["query"]) -> str: ...
    @overload
    def __getitem__(self, key: Literal["hits"]) -> list[_SearchHitRecord]: ...
    @overload
    def __getitem__(self, key: Literal["failure"]) -> _FailureRecord | None: ...

class _ContentMetadataRecord(Protocol):
    start_line: int
    end_line: int
    total_lines: int
    next_offset: int | None
    truncated_by_max_chars: bool
    truncated_mid_line: bool
    partial_line_remaining_chars: int

class _ContentRowRecord(Protocol):
    source: str
    text: str
    title: str
    date: str | None
    failure: _FailureRecord | None
    metadata: _ContentMetadataRecord
    @overload
    def __getitem__(self, key: Literal["source", "text", "title"]) -> str: ...
    @overload
    def __getitem__(self, key: Literal["date"]) -> str | None: ...
    @overload
    def __getitem__(self, key: Literal["failure"]) -> _FailureRecord | None: ...
    @overload
    def __getitem__(self, key: Literal["metadata"]) -> _ContentMetadataRecord: ...

class _ContentBatchRowRecord(_ContentRowRecord, Protocol):
    input_index: int
    @overload
    def __getitem__(self, key: Literal["input_index"]) -> int: ...
    @overload
    def __getitem__(self, key: Literal["source", "text", "title"]) -> str: ...
    @overload
    def __getitem__(self, key: Literal["date"]) -> str | None: ...
    @overload
    def __getitem__(self, key: Literal["failure"]) -> _FailureRecord | None: ...
    @overload
    def __getitem__(self, key: Literal["metadata"]) -> _ContentMetadataRecord: ...

class _ContentFailureRecord(Protocol):
    input_index: int
    source: str
    failure: _FailureRecord

class _ContentMatchRecord(Protocol):
    source: str
    title: str
    line: int
    text: str
    before: list[str]
    after: list[str]
    input_index: int

class _GrepSourceResultRecord(Protocol):
    input_index: int
    source: str
    title: str
    match_count: int
    scan_complete: bool
    failure: _FailureRecord | None

class _GrepReportRecord(Protocol):
    pattern: str
    mode: Literal["regex", "literal"]
    case_sensitive: bool
    context: int
    max_matches_per_source: int
    matches: list[_ContentMatchRecord]
    source_results: list[_GrepSourceResultRecord]
    input_count: int

class _PassageCoordinatesRecord(Protocol):
    start_line: int
    start_character: int
    end_line: int
    end_character: int

class _PassageRecord(Protocol):
    source: str
    title: str
    date: str | None
    text: str
    coordinates: _PassageCoordinatesRecord
    rank: int
    score: float
    ranker: str

class _PassageReportRecord(Protocol):
    query: str
    passages: list[_PassageRecord]
    failures: list[_ContentFailureRecord]
    input_count: int
    unique_source_count: int

class _ExtractionRowRecord(Protocol):
    index: int
    data: dict[str, Any] | None
    failure: _OperationFailureRecord | None
    attempts: int

class _ContractsRecord(Protocol):
    sandbox: int
    capability: int

class _SearchCapabilitiesRecord(Protocol):
    backend: str
    supports_domains: bool
    max_depth: int | None
    limits: Mapping[str, int]

class _ContentCapabilitiesRecord(Protocol):
    url_admission: Literal["searched_only", "searched_or_public_web"]
    limits: Mapping[str, int]

class _LLMCapabilitiesRecord(Protocol):
    available: bool
    limits: Mapping[str, int]

class _MechanismsRecord(Protocol):
    batching: bool
    persistence: bool
    llm_subroutine: bool
    context_decoupling: bool

class _SessionCapabilitiesRecord(Protocol):
    contracts: _ContractsRecord
    search: _SearchCapabilitiesRecord
    content: _ContentCapabilitiesRecord
    llm: _LLMCapabilitiesRecord
    mechanisms: _MechanismsRecord

class _SessionUsageRecord(Protocol):
    exec_calls: int
    search_calls: int
    content_fetches: int
    content_backend_fetches: int
    llm_calls: int
    pipeline_model_tokens: int
    pipeline_output_tokens_reserved: int
    sandbox_seconds: float
    workspace_bytes: int
    documents_seen: int
    budget_consumed: Mapping[str, int | float]
    budget_remaining: Mapping[str, int | float | None]
    provider: Mapping[str, int | float]
    terminal_reason: str | None

class _ReadWindow(TypedDict, total=False):
    source: Required[str]
    offset: int
    limit: int
    max_chars: int

class _SearchResource(Protocol):
    def __call__(
        self,
        query: str,
        *,
        limit: int = ...,
        offset: int = ...,
        domains: list[str] | None = ...,
    ) -> list[_SearchHitRecord]: ...
    def many(
        self,
        queries: list[str],
        *,
        limit_per_query: int = ...,
        offset: int = ...,
        concurrency: int = ...,
        domains: list[str] | None = ...,
    ) -> list[_SearchBatchRecord]: ...
    def fuse_rrf(
        self,
        batches: list[Any],
        *,
        weights: list[float] | None = ...,
        k: int = ...,
        limit: int | None = ...,
        exclude_domains: list[str] | None = ...,
        domain_weights: dict[str, float] | None = ...,
        max_per_domain: int | None = ...,
    ) -> list[_SearchHitRecord]: ...

class _ContentResource(Protocol):
    def get_many(self, sources: list[str]) -> list[_ContentRowRecord]: ...
    def read(
        self,
        source: str,
        *,
        offset: int = ...,
        limit: int = ...,
        max_chars: int = ...,
    ) -> _ContentRowRecord: ...
    def read_many(self, windows: list[_ReadWindow]) -> list[_ContentBatchRowRecord]: ...
    def grep(
        self,
        sources: list[str],
        pattern: str,
        *,
        mode: Literal["regex", "literal"] = ...,
        case_sensitive: bool = ...,
        context: int = ...,
        max_matches_per_source: int = ...,
    ) -> _GrepReportRecord: ...
    def passages(
        self,
        query: str,
        sources: list[str],
        *,
        limit: int = ...,
        max_per_source: int = ...,
    ) -> _PassageReportRecord: ...

class _LLMResource(Protocol):
    def complete(
        self,
        prompt: str,
        *,
        system: str | None = ...,
        temperature: float = ...,
        max_tokens: int | None = ...,
    ) -> str: ...
    def complete_many(
        self,
        prompts: list[str],
        *,
        system: str | None = ...,
        temperature: float = ...,
        max_tokens: int | None = ...,
        concurrency: int = ...,
    ) -> list[str]: ...
    def extract_many(
        self,
        items: list[Any],
        *,
        instruction: str,
        schema: dict[str, Any],
        concurrency: int = ...,
        max_tokens: int | None = ...,
        repair_attempts: int = ...,
    ) -> list[_ExtractionRowRecord]: ...

class _SessionResource(Protocol):
    def usage(self) -> _SessionUsageRecord: ...
    def capabilities(self) -> _SessionCapabilitiesRecord: ...

class _StateResource(Protocol):
    def write_jsonl(self, relative_path: str, rows: list[Any]) -> None: ...
    def append_jsonl(self, relative_path: str, rows: list[Any]) -> None: ...
    def merge_jsonl(
        self,
        relative_path: str,
        rows: list[Any],
        key: str = ...,
    ) -> int: ...
    def exists(self, relative_path: str) -> bool: ...
    def list(self, prefix: str = ...) -> list[str]: ...
    def read_jsonl(self, relative_path: str) -> list[Any]: ...
    def write_json(self, relative_path: str, value: Any) -> None: ...
    def read_json(self, relative_path: str) -> Any: ...

class _OutputResource(Protocol):
    def submit(self, output: Any, *, citations: list[str] | None = ...) -> None: ...

class _SDK(Protocol):
    search: _SearchResource
    content: _ContentResource
    session: _SessionResource
    llm: _LLMResource
    state: _StateResource
    output: _OutputResource

sdk: _SDK
__version__: str

__all__: list[str]
