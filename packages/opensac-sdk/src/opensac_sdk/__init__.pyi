from collections.abc import ItemsView, Iterator, KeysView, Mapping, ValuesView
from typing import Any, Literal, Protocol

class _Record(Protocol):
    def __getitem__(self, key: str) -> Any: ...
    def __iter__(self) -> Iterator[str]: ...
    def __len__(self) -> int: ...
    def get(self, key: str, default: Any = ...) -> Any: ...
    def keys(self) -> KeysView[str]: ...
    def items(self) -> ItemsView[str, Any]: ...
    def values(self) -> ValuesView[Any]: ...

class _OutcomeErrorRecord(_Record, Protocol):
    code: str
    message: str
    retryable: bool
    attempts: int | None
    provider_status: int | None
    retry_after_seconds: float | None
    provider: str | None
    component: str | None
    scope: Literal["request", "resource", "provider", "unknown"] | None

class _Success[ValueT](_Record, Protocol):
    status: Literal["success"]
    value: ValueT
    error: None

class _Failure(_Record, Protocol):
    status: Literal["failure"]
    value: None
    error: _OutcomeErrorRecord

type Outcome[ValueT] = _Success[ValueT] | _Failure

class _FailureRecord(_Record, Protocol):
    code: str
    message: str
    retryable: bool
    attempts: int
    provider_status: int | None
    retry_after_seconds: float | None
    provider: str | None
    component: str | None
    scope: Literal["request", "resource", "provider", "unknown"] | None

class _SearchHitRecord(_Record, Protocol):
    source: str
    backend: str
    title: str
    domain: str | None
    date: str | None
    snippet: str
    score: float | None
    rank: int
    retrieval: Mapping[str, Any] | None
    metadata: dict[str, Any]

class _FusionProvenanceRecord(_Record, Protocol):
    input_index: int
    query: str
    backend: str
    rank: int
    score: float | None

class _FusedSearchHitRecord(_SearchHitRecord, Protocol):
    provenance: list[_FusionProvenanceRecord]
    raw_fused_score: float
    domain_weight: float
    fused_score: float
    fused_rank: int

class _SearchManySuccess(_Success[list[_SearchHitRecord]], Protocol):
    input_index: int
    query: str

class _SearchManyFailure(_Failure, Protocol):
    input_index: int
    query: str

type _SearchManyOutcome = _SearchManySuccess | _SearchManyFailure

class _DocumentRecord(_Record, Protocol):
    source: str
    text: str
    title: str
    date: str | None
    metadata: dict[str, Any]

class _FetchManySuccess(_Success[_DocumentRecord], Protocol):
    input_index: int
    source: str

class _FetchManyFailure(_Failure, Protocol):
    input_index: int
    source: str

type _FetchManyOutcome = _FetchManySuccess | _FetchManyFailure

class _ContentCursorRecord(_Record, Protocol):
    start_line: int
    start_character: int

class _ContentWindowRecord(_Record, Protocol):
    start_line: int | None
    start_character: int
    end_line: int | None
    end_character: int
    total_lines: int
    next: _ContentCursorRecord | None
    truncated_by_max_chars: bool

class _ContentSliceRecord(_DocumentRecord, Protocol):
    window: _ContentWindowRecord

class _ContentFailureRecord(_FailureRecord, Protocol):
    input_index: int
    source: str

class _ContentMatchSpanRecord(_Record, Protocol):
    start_character: int
    end_character: int

class _ContentMatchRecord(_Record, Protocol):
    line: int
    text: str
    before: list[str]
    after: list[str]
    spans: list[_ContentMatchSpanRecord]

class _GrepResultRecord(_Record, Protocol):
    source: str
    title: str | None
    matches: list[_ContentMatchRecord]
    next_start_line: int | None

class _GrepSuccess(_Success[_GrepResultRecord], Protocol):
    input_index: int
    source: str

class _GrepFailure(_Failure, Protocol):
    input_index: int
    source: str

type _GrepOutcome = _GrepSuccess | _GrepFailure

class _PassageCoordinatesRecord(_Record, Protocol):
    start_line: int
    start_character: int
    end_line: int
    end_character: int

class _PassageRecord(_Record, Protocol):
    source: str
    title: str
    date: str | None
    text: str
    coordinates: _PassageCoordinatesRecord
    rank: int
    score: float
    ranker: str

class _PassageReportRecord(_Record, Protocol):
    query: str
    passages: list[_PassageRecord]
    failures: list[_ContentFailureRecord]
    warnings: list[_FailureRecord]
    input_count: int
    unique_source_count: int

class _ExtractManySuccess(_Success[dict[str, Any]], Protocol):
    input_index: int

class _ExtractManyFailure(_Failure, Protocol):
    input_index: int

type _ExtractManyOutcome = _ExtractManySuccess | _ExtractManyFailure

class _ContractsRecord(_Record, Protocol):
    sandbox: int
    capability: int

class _SearchCapabilitiesRecord(_Record, Protocol):
    backend: str
    supports_include_domains: bool
    max_depth: int | None
    limits: Mapping[str, int]

class _ContentCapabilitiesRecord(_Record, Protocol):
    url_admission: Literal["searched_only", "searched_or_public_web"]
    limits: Mapping[str, int]

class _LLMCapabilitiesRecord(_Record, Protocol):
    available: bool
    limits: Mapping[str, int]

class _MechanismsRecord(_Record, Protocol):
    batching: bool
    persistence: bool
    llm_subroutine: bool
    context_decoupling: bool

class _CapabilitiesRecord(_Record, Protocol):
    contracts: _ContractsRecord
    search: _SearchCapabilitiesRecord
    content: _ContentCapabilitiesRecord
    llm: _LLMCapabilitiesRecord
    mechanisms: _MechanismsRecord

class _SearchResource(Protocol):
    def __call__(
        self,
        query: str,
        *,
        limit: int = ...,
        offset: int = ...,
        include_domains: list[str] | None = ...,
    ) -> Outcome[list[_SearchHitRecord]]: ...
    def many(
        self,
        queries: list[str],
        *,
        limit: int = ...,
        offset: int = ...,
        concurrency: int = ...,
        include_domains: list[str] | None = ...,
    ) -> list[_SearchManyOutcome]: ...
    def fuse_rrf(
        self,
        report: list[_SearchManyOutcome],
        *,
        weights: list[float] | None = ...,
        k: int = ...,
        limit: int | None = ...,
        exclude_domains: list[str] | None = ...,
        domain_weights: dict[str, float] | None = ...,
        max_per_domain: int | None = ...,
    ) -> list[_FusedSearchHitRecord]: ...

class _ContentResource(Protocol):
    def fetch(self, source: str) -> Outcome[_DocumentRecord]: ...
    def fetch_many(
        self,
        sources: list[str],
        *,
        concurrency: int = ...,
    ) -> list[_FetchManyOutcome]: ...
    def read(
        self,
        source: str,
        *,
        start_line: int = ...,
        start_character: int = ...,
        line_count: int = ...,
        max_chars: int = ...,
    ) -> Outcome[_ContentSliceRecord]: ...
    def grep(
        self,
        pattern: str,
        *,
        sources: list[str],
        mode: Literal["regex", "literal"] = ...,
        case_sensitive: bool = ...,
        start_line: int = ...,
        context_lines: int = ...,
        limit_per_source: int = ...,
    ) -> list[_GrepOutcome]: ...
    def passages(
        self,
        query: str,
        *,
        sources: list[str],
        limit: int = ...,
        limit_per_source: int = ...,
    ) -> Outcome[_PassageReportRecord]: ...

class _LLMResource(Protocol):
    def complete(
        self,
        prompt: str,
        *,
        system: str | None = ...,
        temperature: float = ...,
        max_tokens: int | None = ...,
    ) -> Outcome[str]: ...
    def extract(
        self,
        item: Any,
        *,
        instruction: str,
        schema: dict[str, Any],
        max_tokens: int | None = ...,
        repair_attempts: int = ...,
    ) -> Outcome[dict[str, Any]]: ...
    def extract_many(
        self,
        items: list[Any],
        *,
        instruction: str,
        schema: dict[str, Any],
        concurrency: int = ...,
        max_tokens: int | None = ...,
        repair_attempts: int = ...,
    ) -> list[_ExtractManyOutcome]: ...

class _CapabilitiesResource(Protocol):
    def __call__(self) -> Outcome[_CapabilitiesRecord]: ...

class _WorkspaceResource(Protocol):
    def write_jsonl(self, relative_path: str, rows: list[Any]) -> None: ...
    def append_jsonl(self, relative_path: str, rows: list[Any]) -> None: ...
    def upsert_jsonl(
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

class _SDK(Protocol):
    search: _SearchResource
    content: _ContentResource
    capabilities: _CapabilitiesResource
    llm: _LLMResource
    workspace: _WorkspaceResource
    def close(self) -> None: ...

sdk: _SDK
__version__: str

__all__: list[str]
