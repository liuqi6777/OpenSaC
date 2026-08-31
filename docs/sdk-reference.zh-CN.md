# OpenSAC SDK 参考

本文档对应 `main` 分支当前捆绑的 `opensac_sdk`。capability contract 为 15，SDK 与 broker
必须匹配；sandbox contract 仍为 14。

## 约定

```python
from opensac_sdk import sdk
```

公开 object 返回值是支持 mapping 的 `Record`。mapping 访问是规范语义；已知且不与 dict 方法
冲突的字段也可用属性读取：

```python
source = row.source
assert source == row["source"]
plain = dict(row)
```

JSON 字段名为 `items`、`values` 或 `get` 时必须使用 `row["..."]`，避免与 dict 方法冲突。

SDK 只检查类型、strict JSON 和基本下界；部署可配置的上限由 broker 强制，并通过
成功的 `sdk.capabilities()` 结果报告。

- 本地参数错误抛 `ValueError`。
- provider、quota、deadline、transport、protocol、permission、抽取 JSON/schema/repair 失败
  返回 `None`，并记录结构化 warning。
- `search.many`、`content.fetch_many` 和 `llm.extract_many` 是公共对齐 fan-out helper；
  每个输入位置返回结果或 `None`。

所有 broker-backed unary 方法返回 `T | None`；对齐 fan-out 返回 `list[T | None]`：

```python
hits = sdk.search("query")
if hits is None:
    print("NEXT: revise the query")
```

必须使用 `is None`，不能依赖 truthiness：`[]`、`""`、`{}` 都可能是成功结果。OpenSAC 会
自动记录有界失败 warning 并由 agent observation 渲染；调用方不需要为 operational failure
写 `try/except` 或手动打印错误。本地参数错误与非预期程序异常仍会传播。

## 公共接口

| Namespace | 操作 |
| --- | --- |
| Search | `search`、`search.many`、`search.fuse_rrf` |
| Content | `content.fetch`、`content.fetch_many`、`content.read`、`content.grep`、`content.passages` |
| LLM | `llm.complete`、`llm.extract`、`llm.extract_many` |
| 顶层 | `capabilities` |
| Workspace | JSON/JSONL 操作，包括 `workspace.upsert_jsonl` |

## Search

### `sdk.search(...)`

```python
sdk.search(
    query: str,
    *,
    limit: int = 10,
    offset: int = 0,
    include_domains: list[str] | None = None,
) -> list[Record] | None
```

`offset` 是完整排名的深度，不是页码。只有 backend 声明支持时才能使用
`include_domains`。

成功结果是排序后的 hit 列表；每个 hit 包含 `source`、
`backend`、`title`、`domain`、`date`、`snippet`、`score`、`rank`、`retrieval` 和 `metadata`。
空列表表示正常零匹配；`None` 表示 operational failure。

### `sdk.search.many(...)`

```python
sdk.search.many(
    queries: list[str],
    *,
    limit: int = 10,
    offset: int = 0,
    concurrency: int = 5,
    include_domains: list[str] | None = None,
) -> list[list[Record] | None]
```

返回列表与 `queries` 一一对齐；成功位置是 hit 列表，失败位置是 `None`：

```python
queries = ["q1", "q2"]
results = sdk.search.many(queries)
for query, hits in zip(queries, results, strict=True):
    if hits is None:
        continue
    print(query, len(hits))
```

Admission、provider、quota、deadline、transport、protocol、contract 和 permission 错误都保留为
对齐 `None`，即使每项都失败也不提升异常；空输入直接返回 `[]`，不调用 broker。结构化 warning
保留 `input_index` 和 query 上下文，返回列表本身不再暴露可编程错误详情。

`Mechanisms.batching` 只控制这个操作。关闭时仍允许单个 query，但拒绝更宽的 fan-out。

实现固定为 SDK 有界线程池路径：先通过 `sdk.capabilities()` 返回的部署 manifest 做 admission，
再为每个输入发出单项 `search.query`，没有环境变量或 broker/client 模式开关。这里的
concurrency 是 helper admission，不是 provider semaphore；预算、rate limit、retry、
cache/coalescing 和实际 provider 并发仍由 broker 控制。SDK 不去重，broker 也不再暴露 batch
search RPC。迁移边界见
[版本说明](opensac-0.8.2.md)。

### `sdk.search.fuse_rrf(...)`

```python
sdk.search.fuse_rrf(
    queries: list[str],
    results: list[list[Record] | None],
    *,
    weights: list[float] | None = None,
    k: int = 60,
    limit: int | None = None,
    exclude_domains: list[str] | None = None,
    domain_weights: dict[str, float] | None = None,
    max_per_domain: int | None = None,
) -> list[Record]
```

这是不调用 broker 的确定性本地 helper。融合结果增加 `provenance`、`raw_fused_score`、
`domain_weight`、`fused_score` 和 `fused_rank`；provenance 行使用 `input_index`，并带
`query`、`backend`、`rank` 和 `score`。queries 与 results 必须等长对齐；`None` 会被跳过，
成功空列表合法，`weights` 仍与所有输入位置对齐。

## Content

Content 操作只接收 source 字符串。本地 document ID 必须由当前 session 的 search 先授权；
web 部署可按宿主策略额外允许受限的公共 HTTP(S) URL。

### `sdk.content.fetch(...)`

```python
sdk.content.fetch(source: str) -> Record | None
```

成功结果是完整规范化文档：`source`、`text`、`title`、`date` 和 provider `metadata`。
同一 source 的重复调用可复用 session cache，但每次请求仍消耗公开的 content fetch budget。

### `sdk.content.fetch_many(...)`

```python
sdk.content.fetch_many(
    sources: list[str],
    *,
    concurrency: int = 5,
) -> list[Record | None]
```

这个 SDK helper 会有界并发调用 unary `content.fetch`。它保留输入顺序和重复 source，不做
capability manifest 预检，空输入直接返回 `[]`。`concurrency` 只限制 SDK worker fan-out；
每个请求仍由 broker 的预算、重试、缓存、trace 和 provider 并发策略管理。

每个输入对应一个对齐结果。需要识别失败 source 时，保留原始输入：

```python
documents = sdk.content.fetch_many(sources)
for source, document in zip(sources, documents, strict=True):
    if document is None:
        continue
    print(source, document.title)
```

所有 operational failure 都保留为逐项 `None`，包括全部为系统性失败的情况；非预期程序异常
原样传播，warning 保留有界的输入上下文。

### `sdk.content.read(...)`

```python
sdk.content.read(
    source: str,
    *,
    start_line: int = 1,
    start_character: int = 0,
    line_count: int = 200,
    max_chars: int = 100_000,
) -> Record | None
```

行号从 1 开始，字符位置从 0 开始且结束位置不包含在内。成功结果包含文档字段和独立的
`window`：

```python
{
    "start_line": int | None,
    "start_character": int,
    "end_line": int | None,
    "end_character": int,
    "total_lines": int,
    "next": {"start_line": int, "start_character": int} | None,
    "truncated_by_max_chars": bool,
}
```

把 `window.next` 原样作为下一次的 `start_line` 和 `start_character`，可以无损续读，包括
`max_chars` 在超长单行中截断的情况。EOF 后返回空文本和 `next=None`；错误坐标抛
`ValueError`。

### `sdk.content.grep(...)`

```python
sdk.content.grep(
    pattern: str,
    *,
    sources: list[str],
    mode: Literal["regex", "literal"] = "regex",
    case_sensitive: bool = False,
    start_line: int = 1,
    context_lines: int = 0,
    limit_per_source: int = 20,
) -> list[Record | None]
```

返回列表与 `sources` 一一对齐。成功结果包含 `source`、`title`、`matches` 和
`next_start_line`；失败 source 的位置是 `None`：

```python
results = sdk.content.grep("target", sources=sources)
for source, result in zip(sources, results, strict=True):
    if result is None:
        continue
    print(source, len(result.matches))
```

match 包含 1-based `line`、`text`、`before`、`after` 和 `spans`。span 使用 0-based、
end-exclusive 的 `start_character`/`end_character`。成功结果的 `next_start_line` 非空时可从
该行继续；为 `None` 表示已扫描到 EOF。成功结果中 `matches` 为空不是失败。

### `sdk.content.passages(...)`

```python
sdk.content.passages(
    query: str,
    *,
    sources: list[str],
    limit: int = 20,
    limit_per_source: int = 3,
) -> Record | None
```

broker 按首次出现顺序去重 source，做全局 passage 排序，再应用单 source 上限。成功报告包含
`query`、`passages`、`failures`、`warnings`、`input_count` 和 `unique_source_count`。
passage 带 source 元数据、精确 `text`、coordinates、`rank`、`score` 和 `ranker`。reranker
失败会退回 lexical BM25，并记录到 `warnings`。

## LLM

Pipeline model 是可选能力；确定性 Python 足够时优先使用 Python。

### `sdk.llm.complete(...)`

```python
sdk.llm.complete(
    prompt: str,
    *,
    system: str | None = None,
    temperature: float = 0.2,
    max_tokens: int | None = None,
) -> str | None
```

### `sdk.llm.extract(...)`

```python
sdk.llm.extract(
    item: Any,
    *,
    instruction: str,
    schema: dict[str, Any],
    max_tokens: int | None = None,
    repair_attempts: int = 0,
) -> dict[str, Any] | None
```

`item` 和 `schema` 必须是 strict-JSON 可序列化值，schema 根必须描述 object。成功结果
是通过 schema 校验的 object。`repair_attempts=1` 允许 broker 做一次 repair；每次 initial 或
repair 尝试都会在模型调用前预留 quota。provider failure、非法/非 object JSON、schema
mismatch、repair 耗尽和 quota exhaustion 都返回 `None`；有界结构化 warning 保留相关详情。

### `sdk.llm.extract_many(...)`

```python
sdk.llm.extract_many(
    items: list[Any],
    *,
    instruction: str,
    schema: dict[str, Any],
    concurrency: int = 4,
    max_tokens: int | None = None,
    repair_attempts: int = 0,
) -> list[dict[str, Any] | None]
```

所有 item 共享 instruction、schema、token 上限和 repair 策略。fan-out 前会先校验全部 item
都是 strict JSON；原始 item 不会复制到诊断中。每项仍是独立的 unary
`llm.extract` 请求，因此 broker quota、重试、trace 和 provider 并发策略保持权威。

返回列表与输入对齐；成功位置是通过 schema 校验的 object，provider、schema、quota、deadline
及系统性失败的位置是 `None`。Warning 带 `input_index`，但有意不记录原始 item。

## Capabilities

### `sdk.capabilities()`

返回 `Record | None`。成功结果包含 contract 版本、search backend 支持、content/LLM
上限和机制开关。生成程序应读取它，不要硬编码部署上限。

## Workspace

Artifact 路径相对 session workspace，不能逃逸：

```python
sdk.workspace.write_json(path, value)
sdk.workspace.read_json(path)
sdk.workspace.write_jsonl(path, rows)
sdk.workspace.append_jsonl(path, rows)
sdk.workspace.upsert_jsonl(path, rows, key="source") -> int
sdk.workspace.read_jsonl(path)
sdk.workspace.exists(path) -> bool
sdk.workspace.list(prefix="") -> list[str]
```

`upsert_jsonl` 保留首次出现顺序，并按 key 替换整个旧行，不做字段级 merge。

使用 Python `print(...)` 返回有界结果，并把精确 source 字符串与对应证据一起输出。更大的
结构化数据应保存到 `sdk.workspace`，不要打印完整文档或 ledger。

## 0.8.4 Breaking 迁移

Broker-backed 调用不再通过 `BrokerError` 暴露 operational failure。旧的直接返回值和
`Outcome`、resource-specific variant 以及旧 `fuse_rrf(report, ...)` 都没有兼容 shim。

| 之前的返回 | 当前返回 |
| --- | --- |
| unary `Outcome[T]` | `T | None` |
| 对齐 `list[Outcome[T]]` | `list[T | None]` |
| `outcome.status` / `.value` / `.error` | `result is None` 加自动结构化 warning |
| `fuse_rrf(search_outcomes, ...)` | `fuse_rrf(queries, search_results, ...)` |

必须使用 `is None`，不能依赖 truthiness。SDK 只捕获 `BrokerError`；本地 `ValueError` 与非预期
程序异常仍会传播。这个生成程序 API 变更不调整 capability contract 15 或 sandbox contract 14。
完整边界见[当前版本说明](opensac-0.8.4.md)。

## 0.8.3 Breaking 迁移

不提供 alias 或弃用 shim。Host usage 计量、execution 记录和 dashboard 指标继续保留在生成程序
SDK 之外。

| 0.8.2 生成程序 API | 0.8.3 替代 |
| --- | --- |
| `sdk.session.usage()` | Host REST、存储或 dashboard 观测 |
| `sdk.session.capabilities()` | `sdk.capabilities()` |
| `sdk.output.submit(...)` | 有界 `print(...)` 与 `sdk.workspace` artifact |
| `sdk.state.*` | `sdk.workspace.*` |

Capability contract 15 下 broker 会拒绝 `session.usage`。Workspace 与 capability namespace
调整不会新增 broker operation；sandbox contract 14 保持不变。完整边界见
[v0.8.3 版本说明](opensac-0.8.3.md)。

## 0.8.2 Breaking 迁移

不提供 broker batch 兼容 handler 或 SDK 模式开关。

| 0.8.1 行为或 API | 0.8.2 替代 |
| --- | --- |
| broker `search.query_many` transport | SDK 有界并发调用 unary `search.query` |
| search 失败 outcome 的 status 包含展示文本 | `status == "failure"` 加结构化 `error` |
| broker-facing `BatchSearchBackend` | unary `SearchBackend.search` |
| `LocalSearchBackend.search_many(...)` | 并发 unary adapter 调用；后端内部 batching 属于私有实现 |

`sdk.search.many` 签名不变。0.8.2 SDK 与 broker 必须配套部署；capability contract 14 会有意
拒绝 0.8.1 wire surface。

## 0.8.1 Breaking 迁移

不提供 alias 或 deprecation shim。

| 删除或重命名 | 0.8.1 替代 |
| --- | --- |
| `search(..., domains=...)` | `include_domains=...` |
| `search.many(..., limit_per_query=...)` | `limit=...` |
| fusion `batch_index` | `input_index` |
| `content.get_many(sources)` | `content.fetch_many(sources)` |
| `content.read(..., offset, limit)` | `start_line`、`start_character`、`line_count` |
| `content.read_many(...)` | 循环调用 `content.read(...)` |
| `content.grep(sources, pattern, context, max_matches_per_source)` | `grep(pattern, sources=..., context_lines=..., limit_per_source=...)` |
| `content.passages(query, sources, max_per_source)` | keyword `sources=...`、`limit_per_source=...` |
| `llm.complete_many(...)` | 循环调用 `llm.complete(...)` |
| 旧 broker `llm.extract_many(...)` | SDK `llm.extract_many(...)` 组合 unary `llm.extract` |
| `state.merge_jsonl(...)` | `workspace.upsert_jsonl(...)` |
