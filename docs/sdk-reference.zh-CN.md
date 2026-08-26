# OpenSAC SDK API 参考

本文档对应仓库当前捆绑的 `opensac_sdk` 0.8.0，覆盖
`SDK_SURFACE` 声明的全部 22 个公共操作。SDK 是同步接口，供 OpenSAC sandbox
中的生成程序调用；宿主与 sandbox 镜像会提供版本匹配的 SDK。

## 1. 入口、返回值与错误

```python
from opensac_sdk import BrokerError, __version__, sdk
```

包根目录只公开 `sdk`、`BrokerError` 和 `__version__`。`sdk` 是懒加载单例，包含
`search`、`content`、`llm`、`session`、`state` 和 `output` 六个 namespace。

Broker 返回的 JSON object 会递归包装成 `Record`。`Record` 是 `dict` 的子类，因此字段支持
属性和 mapping 两种读法：

```python
hits = sdk.search("OpenSAC", limit=3)
source = hits[0].source
assert source == hits[0]["source"]
plain_dict = dict(hits[0])
```

数组仍是普通 `list`，标量仍是普通的 `str`、`int`、`float`、`bool` 或 `None`。

Broker 级失败会抛出 `BrokerError`：

```python
try:
    hits = sdk.search("query")
except BrokerError as error:
    print(
        error.code,
        error.retryable,
        error.attempts,
        error.provider_status,
        error.retry_after_seconds,
        error.provider,
        error.component,
        error.scope,
    )
```

本地参数错误通常抛出 `ValueError`，state 文件不存在会抛出 `FileNotFoundError`。支持局部失败的
批量方法会分别返回 `results` 和 `failures`，两者的 `input_index` 共同覆盖原始输入；`sac_run`
会在 stdout 之前自动展示有界 warning。单项调用失败会抛出 `BrokerError`；合法空结果没有 typed
failure，也不会产生 warning。

参数校验不会进行隐式转换：例如，需要整数时不接受 `"10"`，也不会把 `True` 当作整数。写入
state 或 output 的值必须是严格 JSON；不支持的对象、NaN 和 Infinity 会在已有 artifact 被改动前
抛出 `ValueError`。

### 公共操作总览

| Tier | 操作 |
| --- | --- |
| core | `sdk.search`、`sdk.search.many`、`sdk.content.passages`、`sdk.content.read`、`sdk.content.read_many`、`sdk.content.grep`、`sdk.llm.extract_many`、`sdk.session.usage`、`sdk.session.capabilities`、`sdk.output.submit` |
| helper | `sdk.search.fuse_rrf`、全部 `sdk.state.*` 操作 |
| advanced | `sdk.content.get_many`、`sdk.llm.complete`、`sdk.llm.complete_many` |

`core` 是常规生成程序的首选接口；`helper` 是本地确定性操作；`advanced` 适合确实需要完整内容或
自由文本模型调用的场景。

下文每个方法都使用相同顺序：层级、签名、参数、返回值、行为与异常、示例。

## 2. 公共数据形态

下文使用以下公共 JSON 形态。`?` 表示值可能是 `None`。

### `SearchHit`

```python
{
    "source": str,       # 后续 content 操作使用的唯一公开地址
    "backend": str,
    "title": str,
    "domain": str | None,
    "date": str | None,  # provider 原样给出的日期，未统一格式
    "snippet": str,
    "score": float | None,
    "rank": int,         # 1-based
    "retrieval": {
        "mode": str | None,
        "result_mode": str | None,
        "score_name": str | None,
        "higher_is_better": bool | None,
        "comparable_across_queries": bool | None,
    } | None,
    "metadata": dict,
}
```

### `CapabilityFailure`

```python
{
    "code": str,
    "message": str,
    "retryable": bool,
    "attempts": int,
    "provider_status": int | None,       # 可缺省
    "retry_after_seconds": float | None, # 可缺省
    "provider": str | None,              # 不含密钥的上游名称
    "component": str | None,             # 仅供诊断，例如 document
    "scope": "request" | "resource" | "provider" | "unknown" | None,
}
```

`scope` 表示现有传输证据能够支持的最安全处置层级：`request` 是调用输入问题，`resource` 是单个
query/document 问题，`provider` 是共享服务、凭据或容量问题。状态码无法区分时会明确返回
`unknown`，例如 Jina Reader 的 HTTP 403。SDK 不会暴露 provider 原始响应正文或含凭据的细节。

### `ContentRow`

```python
{
    "source": str,
    "text": str,
    "title": str,
    "date": str | None,
    "metadata": dict,
}
```

该形态只表示抓取成功。单 source 读取失败抛出 `BrokerError`；批量失败使用
`ContentFailure`。

### `ContentFailure`

```python
{
    "input_index": int,
    "source": str,
    # ...CapabilityFailure fields
}
```

### `ContentReadWindow`

```python
{
    "source": str,
    "offset": int,      # 可选，默认 1
    "limit": int,       # 可选，默认 200
    "max_chars": int,   # 可选，默认 100_000
}
```

## 3. `sdk.search`

### `sdk.search(...)`

**层级：** `core`

**签名**

```python
sdk.search(
    query: str,
    *,
    limit: int = 10,
    offset: int = 0,
    domains: list[str] | None = None,
) -> list[Record]
```

**参数**

| 名称 | 默认值 | 说明 |
| --- | --- | --- |
| `query` | 必填 | 非空搜索 query。 |
| `limit` | `10` | 当前排名窗口返回的 hit 数量。 |
| `offset` | `0` | 完整排名中的深度，不是页码。 |
| `domains` | `None` | 可选的 backend 侧域名白名单。 |

**返回值**

`list[Record]`：按 `rank` 排序的 `SearchHit` record。

**行为与异常**

- 返回空列表表示搜索成功但没有命中。
- 只有当前 backend 支持域名过滤时才能使用 `domains`；不支持的过滤请求会失败，不会被忽略。
- `limit` 的有效上限为 100，`offset` 的有效上限为 500；backend 可能施加更小的最大检索深度。
- 整体请求失败时抛出 `BrokerError`。

**示例**

```python
hits = sdk.search("Who introduced ReAct prompting?", limit=10)
sources = [hit.source for hit in hits]
```

### `sdk.search.many(...)`

**层级：** `core`

**签名**

```python
sdk.search.many(
    queries: list[str],
    *,
    limit_per_query: int = 10,
    offset: int = 0,
    concurrency: int = 5,
    domains: list[str] | None = None,
) -> Record
```

**参数**

| 名称 | 默认值 | 说明 |
| --- | --- | --- |
| `queries` | 必填 | 待执行的 query；report outcome 保留原始输入位置。 |
| `limit_per_query` | `10` | 每个 query 的排名窗口大小。 |
| `offset` | `0` | 应用于所有 query 的排名深度。 |
| `concurrency` | `5` | 请求的 broker 侧最大并发数。 |
| `domains` | `None` | 应用于所有 query 的可选域名白名单。 |

**返回值**

`Record`：按输入位置分区的 report：

```python
{
    "results": [
        {"input_index": int, "query": str, "hits": list[SearchHit]}
    ],
    "failures": [
        {"input_index": int, "query": str, ...CapabilityFailure fields}
    ],
    "input_count": int,
}
```

**行为与异常**

- `results` 中的空 `hits` 表示 query 成功但未命中；失败 query 只出现在 `failures` 中。
- `concurrency` 实际限制在 1 到 20。
- 默认部署单次最多接受 64 个 query；该限制可由宿主配置。
- 外部 query 失败（包括成功数为 0/N）会保留 `input_index` 并自动产生 execution warning；无法
  构造安全 report 的失败才抛出 `BrokerError`。

**示例**

```python
report = sdk.search.many(
    ["ReAct paper", "ReAct prompting authors"],
    limit_per_query=10,
    concurrency=2,
)

for failure in report.failures:
    print(failure.query, failure.code)
```

### `sdk.search.fuse_rrf(...)`

**层级：** `helper`

**签名**

```python
sdk.search.fuse_rrf(
    report: Record | dict,
    *,
    weights: list[float] | None = None,
    k: int = 60,
    limit: int | None = None,
    exclude_domains: list[str] | None = None,
    domain_weights: dict[str, float] | None = None,
    max_per_domain: int | None = None,
) -> list[Record]
```

**参数**

| 名称 | 默认值 | 说明 |
| --- | --- | --- |
| `report` | 必填 | `search.many` 返回的 report。 |
| `weights` | `None` | 与 report 原始输入对齐的非负权重。 |
| `k` | `60` | 非负的 RRF 排名平滑常量。 |
| `limit` | `None` | 可选的最终候选数量。 |
| `exclude_domains` | `None` | 需要排除的 hostname 及其子域。 |
| `domain_weights` | `None` | 以 hostname 为 key 的正数分数乘数。 |
| `max_per_domain` | `None` | 每个 Web hostname 的可选上限。 |

**返回值**

`list[Record]`：每个融合候选保留代表性 `SearchHit` 字段，并增加：

```python
{
    # ...SearchHit fields
    "provenance": [
        {
            "batch_index": int,
            "query": str,
            "backend": str,
            "rank": int,
            "score": float | None,
        }
    ],
    "raw_fused_score": float,
    "domain_weight": float,
    "fused_score": float,
    "fused_rank": int,
}
```

**行为与异常**

- 这是确定性的本地 helper，不调用 broker，也不产生 provider 工作。
- 只有 `report.results` 参与融合。`search.many` 已经记录 report 中的失败，因此这个本地 helper
  不会再次解释或产生失败 warning。
- 域名策略匹配指定 hostname 及其子域；非 Web source 不受影响。
- 域名策略在最终 `limit` 之前应用。
- 非法权重、rank、limit 或域名策略会抛出 `ValueError`。

**示例**

```python
fused = sdk.search.fuse_rrf(
    report,
    weights=[1.0, 1.5],
    exclude_domains=["social.example"],
    domain_weights={"docs.example.com": 2.0},
    max_per_domain=3,
    limit=20,
)
```

## 4. `sdk.content`

content 方法只接受 source 字符串，不能直接传 `SearchHit` record。`read` 只接受一个 source；
批量方法接受 `list[str]`，而 `read_many` 接受 `ContentReadWindow` object 列表。每个 source 最长
4096 字符；默认部署单次最多接受 256 个批量项，但该值可由宿主配置。

Web 部署可能根据宿主策略直接接受公开 HTTP(S) URL；本地 document ID 必须先由当前 session 的
search 返回。`get_many`、`read_many` 和 `grep` 会保留重复 source 的输入位置，同时允许 broker
复用抓取结果；`passages` 会按首次出现位置去重。

### `sdk.content.get_many(...)`

**层级：** `advanced`

**签名**

```python
sdk.content.get_many(sources: list[str]) -> Record
```

**参数**

| 名称 | 默认值 | 说明 |
| --- | --- | --- |
| `sources` | 必填 | 按输入顺序抓取的 source 字符串。 |

**返回值**

`Record`：`results` 包含成功的完整文档，`failures` 包含扁平 `ContentFailure`，并提供
`input_count`。两类 outcome 都带 `input_index`。

**行为与异常**

- 重复输入保留独立结果位置，但抓取工作可以复用。
- 证据发现优先使用 `passages`，有界行窗口优先使用 `read`。
- 外部抓取失败（包括成功数为 0/N）会保留 `input_index` 并自动产生 execution warning。

**示例**

```python
report = sdk.content.get_many([hit.source for hit in hits])
for row in report.results:
    process(row.text)
```

### `sdk.content.read(...)`

**层级：** `core`

**签名**

```python
sdk.content.read(
    source: str,
    *,
    offset: int = 1,
    limit: int = 200,
    max_chars: int = 100_000,
) -> Record
```

**参数**

| 名称 | 默认值 | 说明 |
| --- | --- | --- |
| `source` | 必填 | 要读取的单个 source 字符串。 |
| `offset` | `1` | 窗口首行；行号从 1 开始。 |
| `limit` | `200` | 每个 source 的最大行数。 |
| `max_chars` | `100_000` | 每个 source 的最大返回字符数。 |

**返回值**

`Record`：单个 `ContentRow`。成功行的 `metadata` 至少增加：

```python
{
    "start_line": int,       # 空窗口时为 0
    "end_line": int,
    "total_lines": int,
    "next_offset": int | None,
    "truncated_by_max_chars": bool,        # 仅截断时出现
    "truncated_mid_line": bool,            # 仅单行过长时出现
    "partial_line_remaining_chars": int,   # 仅单行中途截断时出现
}
```

**行为与异常**

- 有效值会被约束到 `offset >= 1`、`1 <= limit <= 5000`、`1 <= max_chars <= 400000`。
- `next_offset is None` 表示已经到达文档末尾。
- 外部抓取失败会抛出 `BrokerError`，不会伪造空文本成功行。

**示例**

```python
offset = 1
while offset is not None:
    row = sdk.content.read(source, offset=offset, limit=200)
    consume(row.text)
    offset = row.metadata.next_offset
```

### `sdk.content.read_many(...)`

**层级：** `core`

**签名**

```python
sdk.content.read_many(windows: list[ContentReadWindow]) -> Record
```

**参数**

| 名称 | 默认值 | 说明 |
| --- | --- | --- |
| `windows` | 必填 | 按输入顺序排列的逐 source 读取窗口；未知字段会被拒绝。 |

**返回值**

`Record`：`results` 包含成功的 `ContentRow`，`failures` 包含扁平 `ContentFailure`，并提供
`input_count`。两类 outcome 都带 `input_index`；每个 window 独立使用自己的 `offset`、`limit`
和 `max_chars`。

**行为与异常**

- 重复 source 保留独立结果位置和切片，broker 可以复用抓取工作。
- 缺省的 window 参数使用与 `read` 相同的默认值和边界。
- window 非法时抛出 `ValueError`；外部抓取失败会保留 `input_index` 并自动产生 execution warning。

**示例**

```python
report = sdk.content.read_many(
    [
        {"source": first_source, "offset": 1, "limit": 80},
        {"source": second_source, "offset": 120, "limit": 40, "max_chars": 16_000},
    ]
)
```

### `sdk.content.grep(...)`

**层级：** `core`

**签名**

```python
sdk.content.grep(
    sources: list[str],
    pattern: str,
    *,
    mode: Literal["regex", "literal"] = "regex",
    case_sensitive: bool = False,
    context: int = 0,
    max_matches_per_source: int = 20,
) -> Record
```

**参数**

| 名称 | 默认值 | 说明 |
| --- | --- | --- |
| `sources` | 必填 | 按输入顺序检查的 source 字符串。 |
| `pattern` | 必填 | 非空匹配模式。 |
| `mode` | `"regex"` | 显式匹配模式：`"regex"` 或 `"literal"`。 |
| `case_sensitive` | `False` | 是否区分大小写。 |
| `context` | `0` | 匹配行前后各返回的上下文行数。 |
| `max_matches_per_source` | `20` | 每个输入 source 的最大匹配数。 |

**返回值**

`Record`：以下形态的 grep report：

```python
{
    "pattern": str,
    "mode": "regex" | "literal",
    "case_sensitive": bool,
    "context": int,
    "max_matches_per_source": int,
    "matches": [
        {
            "source": str,
            "title": str,
            "line": int,          # 1-based，可直接作为 read(offset=...)
            "text": str,
            "before": list[str],
            "after": list[str],
            "input_index": int,
        }
    ],
    "source_results": [
        {
            "input_index": int,
            "source": str,
            "title": str,
            "match_count": int,
            "scan_complete": bool,
        }
    ],
    "failures": [ContentFailure],
    "input_count": int,
}
```

**行为与异常**

- 按行执行匹配。regex 模式下非法正则会抛出 `ValueError`；literal 模式不会解释正则元字符。
- `context` 实际限制在 0 到 20；`max_matches_per_source` 实际限制在 1 到 200。
- `source_results` 只包含成功扫描。`scan_complete=True` 且 `match_count=0` 表示成功的零匹配；
  达到上限的成功扫描会标记为 `scan_complete=False`，抓取失败则单独位于 `failures`。
- 重复 source 可通过 `input_index` 区分。
- 外部抓取失败（包括成功 scan 数为 0/N）按 `input_index` 保留在 `failures` 中并自动产生
  execution warning。

**示例**

```python
report = sdk.content.grep(sources, r"born in \d{4}", context=2)
for match in report.matches:
    window = sdk.content.read(
        match.source,
        offset=max(1, match.line - 5),
        limit=11,
    )
```

### `sdk.content.passages(...)`

**层级：** `core`

**签名**

```python
sdk.content.passages(
    query: str,
    sources: list[str],
    *,
    limit: int = 20,
    max_per_source: int = 3,
) -> Record
```

**参数**

| 名称 | 默认值 | 说明 |
| --- | --- | --- |
| `query` | 必填 | 非空的 passage 排序 query。 |
| `sources` | 必填 | 调用者授权的 source 字符串。 |
| `limit` | `20` | 整份 report 的最大 passage 数。 |
| `max_per_source` | `3` | 单个 source 的最大 passage 数。 |

**返回值**

`Record`：以下形态的 passage report：

```python
{
    "query": str,
    "passages": [
        {
            "source": str,
            "title": str,
            "date": str | None,
            "text": str,
            "coordinates": {
                "start_line": int,       # 1-based
                "start_character": int,  # 0-based
                "end_line": int,         # 1-based
                "end_character": int,    # 0-based，exclusive
            },
            "rank": int,
            "score": float,
            "ranker": str,
        }
    ],
    "failures": list[ContentFailure],
    "warnings": list[CapabilityFailure], # reranker fallback 诊断
    "input_count": int,
    "unique_source_count": int,
}
```

**行为与异常**

- 排序前按 source 首次出现的位置去重。
- `limit` 必须在 1 到 100 之间；`max_per_source` 必须在 1 到 10 之间。
- `score` 只在同一份 report 内部可比较。
- `coordinates` 是标准化文档文本中的半开区间。
- 抓取失败进入 `failures` 并自动产生 execution warning，包括没有任何 source 成功的情况。
- 配置的 reranker 失败时回退到 `lexical:bm25`；typed 诊断进入 `warnings`，并在 stdout 前展示。

**示例**

```python
report = sdk.content.passages(
    "original authors and publication date",
    sources,
    limit=20,
    max_per_source=3,
)
```

## 5. `sdk.llm`

这些方法使用宿主可选配置的 pipeline model。部署未配置模型、整个 provider 调用失败或预算不足时
会抛出 `BrokerError`。能用确定性 Python 完成的处理应优先使用 Python；需要结构化输出时优先使用
`extract_many`。

### `sdk.llm.complete(...)`

**层级：** `advanced`

**签名**

```python
sdk.llm.complete(
    prompt: str,
    *,
    system: str | None = None,
    temperature: float = 0.2,
    max_tokens: int | None = None,
) -> str
```

**参数**

| 名称 | 默认值 | 说明 |
| --- | --- | --- |
| `prompt` | 必填 | 非空的用户 prompt。 |
| `system` | `None` | 可选的 system instruction。 |
| `temperature` | `0.2` | 采样温度。 |
| `max_tokens` | `None` | 可选的 completion token 上限。 |

**返回值**

`str`：模型响应文本。

**行为与异常**

- `temperature` 实际限制在 0.0 到 2.0。
- `max_tokens` 实际限制在 1 到 32000，并可能被 session 预算进一步降低。
- 未配置模型、provider 失败或预算耗尽时抛出 `BrokerError`。

**示例**

```python
summary = sdk.llm.complete(
    "Summarize this evidence:\n" + evidence,
    system="Be concise and preserve numbers.",
    max_tokens=300,
)
```

### `sdk.llm.complete_many(...)`

**层级：** `advanced`

**签名**

```python
sdk.llm.complete_many(
    prompts: list[str],
    *,
    system: str | None = None,
    temperature: float = 0.2,
    max_tokens: int | None = None,
    concurrency: int = 4,
) -> list[str]
```

**参数**

| 名称 | 默认值 | 说明 |
| --- | --- | --- |
| `prompts` | 必填 | 非空 prompt；结果保持输入顺序。 |
| `system` | `None` | 所有 prompt 共用的可选 system instruction。 |
| `temperature` | `0.2` | 所有 prompt 共用的采样温度。 |
| `max_tokens` | `None` | 每个响应可选的 completion token 上限。 |
| `concurrency` | `4` | 请求的最大模型并发数。 |

**返回值**

`list[str]`：每个输入 prompt 对应一个响应字符串。

**行为与异常**

- 空输入返回 `[]`；单个 prompt 不允许为空。
- `concurrency` 实际限制在 1 到 12。
- 未配置模型、batch provider 失败或预算耗尽时抛出 `BrokerError`。

**示例**

```python
summaries = sdk.llm.complete_many(prompts, concurrency=4, max_tokens=200)
```

### `sdk.llm.extract_many(...)`

**层级：** `core`

**签名**

```python
sdk.llm.extract_many(
    items: list[Any],
    *,
    instruction: str,
    schema: dict[str, Any],
    concurrency: int = 4,
    max_tokens: int | None = None,
    repair_attempts: int = 0,
) -> Record
```

**参数**

| 名称 | 默认值 | 说明 |
| --- | --- | --- |
| `items` | 必填 | 严格 JSON 可序列化的输入。 |
| `instruction` | 必填 | 所有 item 共用的提取指令。 |
| `schema` | 必填 | 受支持的 object-root JSON Schema。 |
| `concurrency` | `4` | 请求的最大模型并发数。 |
| `max_tokens` | `None` | 每次尝试可选的 completion token 上限。 |
| `repair_attempts` | `0` | 额外修复次数，只能是 `0` 或 `1`。 |

**返回值**

`Record`：按输入位置分区的提取 report：

```python
{
    "results": [
        {"input_index": int, "data": dict, "attempts": int}
    ],
    "failures": [
        {"input_index": int, "attempts": int, ...CapabilityFailure fields}
    ],
    "input_count": int,
}
```

**行为与异常**

- 成功与失败行彼此分离；两者的 `input_index` 共同覆盖输入。
- `repair_attempts=1` 会为可修复的格式或 schema 错误追加一次修复。
- `items` 和 `schema` 不接受 NaN 或 Infinity；schema 根节点必须声明 `{"type": "object"}`。
- 支持的 schema 关键词为 `$schema`、`type`、`properties`、`required`、
  `additionalProperties`、`items`、`enum` 和 `description`。
- 默认部署最多处理 256 个 item；大小与嵌套深度限制可由宿主配置。
- 单项 provider 失败可与成功行并存；如果所有项都失败，report 仍返回全部 failure，`sac_run`
  会展示 0/N execution warning。

**示例**

```python
report = sdk.llm.extract_many(
    passages,
    instruction="Extract whether the passage names an author.",
    schema={
        "type": "object",
        "properties": {
            "has_author": {"type": "boolean"},
            "author": {"type": ["string", "null"]},
        },
        "required": ["has_author", "author"],
        "additionalProperties": False,
    },
    repair_attempts=1,
)
```

## 6. `sdk.session`

### `sdk.session.usage()`

**层级：** `core`

**签名**

```python
sdk.session.usage() -> dict[str, Any]
```

**参数**

无。

**返回值**

`Record`：当前 session 的策略用量、剩余额度和终止状态：

```python
{
    "exec_calls": int,
    "search_calls": int,
    "content_fetches": int,
    "content_backend_fetches": int,
    "direct_url_attempts": int,
    "direct_url_successes": int,
    "llm_calls": int,
    "pipeline_model_tokens": int,
    "pipeline_output_tokens_reserved": int,
    "sandbox_seconds": float,
    "workspace_bytes": int,
    "documents_seen": int,
    "budget_consumed": {
        "max_exec_calls": int,
        "max_search_queries": int,
        "max_content_fetches": int,
        "max_pipeline_llm_calls": int,
        "max_pipeline_output_tokens": int,
        "max_sandbox_seconds": float,
        "max_workspace_bytes": int,
    },
    "budget_remaining": {
        "max_exec_calls": int | None,
        "max_search_queries": int | None,
        "max_content_fetches": int | None,
        "max_pipeline_llm_calls": int | None,
        "max_pipeline_output_tokens": int | None,
        "max_sandbox_seconds": float | None,
        "max_workspace_bytes": int | None,
    },
    "provider": {
        "attempts_by_capability": dict[str, int],
        "retries": int,
        "intra_call_deduplicated_items": int,
        "coalesced_requests": int,
        "queue_seconds": float,
        "rate_limit_wait_seconds": float,
        "backoff_seconds": float,
    },
    "terminal_reason": str | None,
}
```

**行为与异常**

- `budget_remaining` 中的 `None` 表示该资源未设置硬上限。
- `provider.attempts_by_capability` 按调用方 Capability family（如 `search`、`content`、
  `llm`）归属 backend attempt；未出现的 family 表示没有发起 backend 调用。
- broker 读取失败时抛出 `BrokerError`。

**示例**

```python
usage = sdk.session.usage()
if usage.budget_remaining.max_search_queries == 0:
    stop_searching()
```

### `sdk.session.capabilities()`

**层级：** `core`

**签名**

```python
sdk.session.capabilities() -> dict[str, Any]
```

**参数**

无。

**返回值**

`Record`：session 的公共契约版本、当前 search backend 与限制、content 策略与限制、结构化提取
可用性与限制，以及启用的 mechanisms：

```python
{
    "contracts": {"sandbox": int, "capability": int},
    "search": {"backend": str, "supports_domains": bool, "max_depth": int | None, "limits": dict},
    "content": {"url_admission": str, "limits": dict},
    "llm": {"available": bool, "limits": dict},
    "mechanisms": dict,
}
```

**行为与异常**

- manifest 由宿主当前实际配置构建，不包含凭证或 provider secret。
- broker 读取失败时抛出 `BrokerError`。

**示例**

```python
capabilities = sdk.session.capabilities()
batch_limit = capabilities.content.limits.max_sources_per_request
```

## 7. `sdk.state`

state 方法都是本地文件操作，不调用 broker。路径相对于当前 session workspace，不能通过
`..` 等方式逃逸。state 是 live session 的程序记忆，不是跨 session 的数据库；宿主报告
`state_lost` 后，本地 document source 也会失效。

### `sdk.state.write_jsonl(...)`

**层级：** `helper`

**签名**

```python
sdk.state.write_jsonl(relative_path: str, rows: list[Any]) -> None
```

**参数**

| 名称 | 默认值 | 说明 |
| --- | --- | --- |
| `relative_path` | 必填 | workspace 相对 JSONL 路径。 |
| `rows` | 必填 | 序列化为 JSONL 行的值。 |

**返回值**

`None`。

**行为与异常**

- 严格编码全部 row 后原子替换文件，并自动创建缺失的父目录；序列化失败时已有文件不变。
- 路径逃逸 workspace 时抛出 `ValueError`。

**示例**

```python
sdk.state.write_jsonl("queries.jsonl", [{"query": "alpha"}])
```

### `sdk.state.append_jsonl(...)`

**层级：** `helper`

**签名**

```python
sdk.state.append_jsonl(relative_path: str, rows: list[Any]) -> None
```

**参数**

| 名称 | 默认值 | 说明 |
| --- | --- | --- |
| `relative_path` | 必填 | workspace 相对 JSONL 路径。 |
| `rows` | 必填 | 追加为 JSONL 行的值。 |

**返回值**

`None`。

**行为与异常**

- 严格编码完整输入后再追加且不去重；序列化失败不会追加任何内容，文件和父目录不存在时自动创建。
- 路径逃逸 workspace 时抛出 `ValueError`。

**示例**

```python
sdk.state.append_jsonl("queries.jsonl", [{"query": "beta"}])
```

### `sdk.state.merge_jsonl(...)`

**层级：** `helper`

**签名**

```python
sdk.state.merge_jsonl(
    relative_path: str,
    rows: list[Any],
    key: str = "source",
) -> int
```

**参数**

| 名称 | 默认值 | 说明 |
| --- | --- | --- |
| `relative_path` | 必填 | workspace 相对 JSONL 路径。 |
| `rows` | 必填 | 待 upsert 的 object row。 |
| `key` | `"source"` | 标识相同逻辑 row 的字段。 |

**返回值**

`int`：合并后的总 row 数。

**行为与异常**

- 重复 key 会替换对应 row，但不会改变 key 首次出现的顺序；替换是原子的。
- 每个新 row 都必须是包含 `key` 的 object。
- 缺少 key 或路径逃逸 workspace 时抛出 `ValueError`。

**示例**

```python
count = sdk.state.merge_jsonl("pool.jsonl", hits, key="source")
```

### `sdk.state.exists(...)`

**层级：** `helper`

**签名**

```python
sdk.state.exists(relative_path: str) -> bool
```

**参数**

| 名称 | 默认值 | 说明 |
| --- | --- | --- |
| `relative_path` | 必填 | 待检查的 workspace 相对路径。 |

**返回值**

`bool`：仅当路径是已存在的文件时返回 `True`。

**行为与异常**

- 目录和不存在的路径返回 `False`。
- 路径逃逸 workspace 时抛出 `ValueError`。

**示例**

```python
if sdk.state.exists("pool.jsonl"):
    pool = sdk.state.read_jsonl("pool.jsonl")
```

### `sdk.state.list(...)`

**层级：** `helper`

**签名**

```python
sdk.state.list(prefix: str = "") -> list[str]
```

**参数**

| 名称 | 默认值 | 说明 |
| --- | --- | --- |
| `prefix` | `""` | 应用于 workspace 相对路径的字符串前缀。 |

**返回值**

`list[str]`：排序后的 workspace 相对文件路径。

**行为与异常**

- 递归搜索；workspace 不存在时返回 `[]`。
- `.opensac-` 开头的运行时文件不会出现。

**示例**

```python
artifacts = sdk.state.list("pool")
```

### `sdk.state.read_jsonl(...)`

**层级：** `helper`

**签名**

```python
sdk.state.read_jsonl(relative_path: str) -> list[Any]
```

**参数**

| 名称 | 默认值 | 说明 |
| --- | --- | --- |
| `relative_path` | 必填 | workspace 相对 JSONL 路径。 |

**返回值**

`list[Any]`：非空 JSONL 行，其中的 object 会递归包装为 `Record`。

**行为与异常**

- 忽略空行。
- 文件不存在时抛出 `FileNotFoundError`；行不是合法 JSON 或路径逃逸时抛出 `ValueError`。

**示例**

```python
pool = sdk.state.read_jsonl("pool.jsonl")
```

### `sdk.state.write_json(...)`

**层级：** `helper`

**签名**

```python
sdk.state.write_json(relative_path: str, value: Any) -> None
```

**参数**

| 名称 | 默认值 | 说明 |
| --- | --- | --- |
| `relative_path` | 必填 | workspace 相对 JSON 路径。 |
| `value` | 必填 | 待序列化的值。 |

**返回值**

`None`。

**行为与异常**

- 严格编码 value 后原子替换文件，并自动创建缺失的父目录；序列化失败时已有文件不变。
- 路径逃逸 workspace 时抛出 `ValueError`。

**示例**

```python
sdk.state.write_json("progress.json", {"offset": 20})
```

### `sdk.state.read_json(...)`

**层级：** `helper`

**签名**

```python
sdk.state.read_json(relative_path: str) -> Any
```

**参数**

| 名称 | 默认值 | 说明 |
| --- | --- | --- |
| `relative_path` | 必填 | workspace 相对 JSON 路径。 |

**返回值**

`Any`：解析后的 JSON，其中的 object 会递归包装为 `Record`。

**行为与异常**

- 文件不存在时抛出 `FileNotFoundError`；JSON 非法或路径逃逸时抛出 `ValueError`。

**示例**

```python
progress = sdk.state.read_json("progress.json")
```

## 8. `sdk.output`

### `sdk.output.submit(...)`

**层级：** `core`

**签名**

```python
sdk.output.submit(
    output: Any,
    *,
    citations: list[str] | None = None,
) -> None
```

**参数**

| 名称 | 默认值 | 说明 |
| --- | --- | --- |
| `output` | 必填 | 待序列化的最终值。 |
| `citations` | `None` | 调用者声明的可选 URL/source 字符串。 |

**返回值**

`None`。该方法原子写入以下最终结果 artifact：

```python
{
    "output": Any,
    "citations": list[str],
}
```

**行为与异常**

- `citations` 最多接受 256 个字符串，每个最长 4096 字符。
- citation 是未经验证的标签；该方法不会抓取、解析或验证来源是否支持答案。
- 后一次调用会原子替换前一次提交。
- citation 形态非法或 output 不是严格 JSON（包括 NaN 和 Infinity）时抛出 `ValueError`，已有
  提交保持不变。

**示例**

```python
sdk.output.submit(
    {"answer": answer, "confidence": 0.9},
    citations=[passage.source for passage in report.passages],
)
```

## 9. 生命周期与非公共入口

`sdk.close()` 会关闭懒加载 client 当前持有的 broker transport；后续再次访问 namespace 时会按
环境变量重新创建 client。普通 sandbox 程序通常不需要显式调用，进程退出时 SDK 会自动关闭。

`StateResource.from_environment()`、`OutputResource.from_environment()`、transport 构造器和各资源类
属于 SDK 实现细节，不在 `SDK_SURFACE` 公共操作清单中。不要从内部模块导入这些类型；公共入口
保持为：

```python
from opensac_sdk import BrokerError, __version__, sdk
```
