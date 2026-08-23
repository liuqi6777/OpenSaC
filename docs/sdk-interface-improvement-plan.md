# OpenSAC SDK 接口改进计划

## 状态

- 状态：Implemented，已在 `0.6.4` 完成。
- 原始基线：OpenSAC/SDK `0.6.3`。
- 实现版本：OpenSAC/SDK `0.6.4`。
- 原始 contract：`SANDBOX_CONTRACT=9`、`capability_contract=8`。
- 实现 contract：`SANDBOX_CONTRACT=10`、`capability_contract=9`。

本文档记录本轮 SDK 接口改进的目标契约、兼容性边界、实施顺序、迁移方式和验收标准。
它不是逐方法的当前行为说明；当前 API 以 `docs/sdk-reference.md` 和
`docs/sdk-reference.zh-CN.md` 为准。

## 1. 背景与问题

0.6 系列已经完成了最重要的 surface 收敛：SDK 只有六个 namespace、20 个公共 operation，文档
source 统一为字符串，结构化结果保持 JSON record，整次 RPC 失败与逐项失败也有明确边界。这些
方向应当保留。

当前剩余问题集中在契约一致性和运行时可发现性，而不是 operation 数量：

1. `state` 和 `output` 使用 `json.dumps(..., default=str)`，非 JSON 值会被静默转换，NaN/Infinity
   也可能进入 artifact；`llm.extract_many` 却使用严格 JSON，三者语义不一致。
2. backend 是否支持 domain filter、content 是否允许直接 URL、pipeline LLM 是否可用，以及动态
   batch 上限都已存在于宿主 environment manifest，但 sandbox SDK 无法读取，只能通过失败试探。
3. SDK 运行时返回 `Record`，但 wheel 没有 `py.typed` 和 shape-aware stub；IDE 和静态检查只能把
   record 字段视为 `Any`。
4. `content.read()` 接受 source list，却只能应用同一组 `offset/limit/max_chars`，同时返回 list；
   单条读取因此也要传 list、取 `[0]`。从多个 grep match 扩展不同窗口时又必须循环发起多个 RPC。
5. 参数越界有时严格拒绝，有时单边 clamp，有时双边 clamp；部分字符串参数还会被 `str(...)`
   隐式转换。调用者无法从签名预测错误行为。
6. 逐项失败分别使用 `failure`、`failures` 和 `error`；某些 fallback failure 缺少稳定的可选字段。
7. `session.usage()` 暴露的字段无法完整解释 budget remaining，尤其缺少实际用于扣减预算的
   `pipeline_output_tokens_reserved`，也隐藏了 cache/provider 层用量。
8. `grep_report` 方法名暴露了返回容器而不是动作；当前 report 也无法表达每个 source 是否因为
   `max_matches_per_source` 提前停止扫描，持久化后还缺少 pattern/mode/context 等自描述信息。

## 2. 目标

1. 所有正式 JSON artifact 使用严格、可预测的 JSON 语义。
2. 让 sandbox 程序在调用 capability 前读取当前 session 的功能、限制和可选机制。
3. 在不引入公共 Pydantic 模型和运行时依赖的前提下，提供字段级静态类型。
4. 将 `read` 收缩为单 source/单 record，并用 `read_many` 支持不同 source 的不同 line window，
   保持输入对齐和逐项失败。
5. 对同类参数采用统一的本地与 broker 双层验证，拒绝静默类型转换和非文档化 clamp。
6. 保留领域专属 report，同时统一逐项失败的名称和基础字段。
7. 让 usage、budget consumed 和 budget remaining 可以对账。
8. 将 `grep_report` 收敛为 `grep`，保留 flat match 遍历，同时让每个输入 source 的扫描状态可见。
9. 保持 SDK core profile 小于等于 12 个 model-facing operation。

## 3. 非目标

- 不恢复公共 Pydantic model hierarchy。
- 不新增 `opensac-protocol` 发布包或 host/SDK 运行时依赖。
- 不把所有返回值合并成包含大量 optional 字段的通用 `Result`。
- 不增加 async SDK；批量并发继续由 broker capability 负责。
- 不改变 `source` 的寻址、session admission、sandbox 隔离或 provider credential 边界。
- 不允许生成程序绕过 broker 直接访问网络。
- 不在本轮增加 claim-level citation 或修改 `output.submit(citations=list[str])` 的证据语义。
- 不重写历史版本文档来伪装旧版本已经具备新接口。
- 不改变 search/reranker/provider 的质量策略。

## 4. 设计原则

### 4.1 保持小 surface

本轮只增加两个公共 operation：

```text
sdk.session.capabilities
sdk.content.read_many
```

目标公共 operation 数从 20 增加到 22；model core 从当前 9 增加到 11，仍低于既有上限 12。

### 4.2 保持 JSON/Record runtime

宿主继续用 Pydantic 校验 wire payload，SDK 继续递归包装 JSON object 为轻量 `Record`。静态类型
通过 `.pyi` 和 `py.typed` 提供，不改变运行时对象，不向包根增加模型导出。

包根仍必须严格保持：

```python
__all__ = ["BrokerError", "sdk", "__version__"]
```

### 4.3 Host 与 SDK 双层验证

SDK 在 RPC 前给出快速、确定的 `ValueError`；broker 重复执行安全校验，不能信任 SDK。两层必须由
contract test 证明接受和拒绝相同的公开范围。

### 4.4 明确 breaking change

不为已发布的错误字段名、silent clamp 或宽松 JSON 长期保留隐式 shim。0.6.4 release note 必须列出
迁移步骤；版本匹配的 host/sandbox 镜像必须作为一个整体发布。

## 5. 目标接口

### 5.1 严格 JSON artifact

以下方法统一使用同一个私有 strict JSON encoder：

```text
sdk.state.write_jsonl
sdk.state.append_jsonl
sdk.state.merge_jsonl
sdk.state.write_json
sdk.output.submit
```

编码规则：

- `allow_nan=False`；
- 不使用 `default=str`；
- 支持普通 JSON value 和 SDK `Record`；
- 非 JSON value、NaN、Infinity、循环引用都在写文件前抛出 `ValueError`；
- 错误信息包含字段/row index，但不得包含完整文档内容或敏感值；
- 使用 UTF-8，建议 `ensure_ascii=False` 保持 artifact 可读；
- `write_json`、`write_jsonl`、`merge_jsonl` 和 `output.submit` 使用临时文件加原子替换；
- `append_jsonl` 保持 append 语义，不为原子替换重写整个事件文件。

示例迁移：

```python
# 0.6：datetime 被静默转成字符串
sdk.state.write_json("progress.json", {"updated_at": datetime.now()})

# 0.6.4：调用者显式决定表示方式
sdk.state.write_json(
    "progress.json",
    {"updated_at": datetime.now().isoformat()},
)
```

### 5.2 `sdk.session.capabilities()`

目标签名：

```python
sdk.session.capabilities() -> Record
```

目标返回形态：

```python
{
    "contracts": {
        "sandbox": 10,
        "capability": 9,
    },
    "search": {
        "backend": str,
        "supports_domains": bool,
        "max_depth": int | None,
        "limits": {
            "max_queries_per_request": int,
            "max_query_chars": int,
            "max_top_k": int,
            "max_limit": 100,
            "max_offset": 500,
            "max_concurrency": 20,
        },
    },
    "content": {
        "url_admission": "searched_only" | "searched_or_public_web",
        "limits": {
            "max_sources_per_request": int,
            "read_max_lines": 5000,
            "read_max_chars": 400000,
            "grep_max_context": 20,
            "grep_max_matches_per_source": 200,
            "passage_limit": 100,
            "passage_max_per_source": 10,
        },
    },
    "llm": {
        "available": bool,
        "limits": {
            "max_concurrency": 12,
            "max_completion_tokens": 32000,
            "extract_max_items": int,
            "extract_max_instruction_bytes": int,
            "extract_max_schema_bytes": int,
            "extract_max_item_bytes": int,
            "extract_max_total_item_bytes": int,
            "extract_max_schema_depth": int,
            "extract_max_repair_attempts": int,
        },
    },
    "mechanisms": {
        "batching": bool,
        "persistence": bool,
        "llm_subroutine": bool,
        "context_decoupling": bool,
    },
}
```

约束：

- 返回 session 可见的能力，不返回 API key、provider URL、model name、worker path 或其他运维秘密；
- 动态 session mechanism 必须反映本 session，而不是进程默认值；
- API environment manifest 与 SDK capabilities 的公共限制从同一个内部 builder 生成，禁止复制两套
  hard-coded constants；
- `llm.available=False` 时仍返回公开 limits，便于程序区分“不可用”与“参数越界”；
- 该调用不计入 search/content/LLM usage。

### 5.3 Type-only contracts

新增：

```text
packages/opensac-sdk/src/opensac_sdk/py.typed
packages/opensac-sdk/src/opensac_sdk/__init__.pyi
```

`__init__.pyi` 描述 `sdk` 六个 namespace、22 个公共 operation、`BrokerError` 字段，以及以下稳定
record shape：

```text
SearchHitRecord
SearchBatchRecord
ContentRowRecord
ContentMatchRecord
ContentFailureRecord
GrepSourceResultRecord
GrepReportRecord
PassageRecord
PassageReportRecord
ExtractionRowRecord
SessionCapabilitiesRecord
SessionUsageRecord
```

这些名字只服务静态分析，可以在 stub 内保持私有；运行时不新增 importable model。shape-specific
record stub 同时声明 attribute read 和 mapping read，使以下两种写法都能通过类型检查：

```python
source_a: str = hit.source
source_b: str = hit["source"]
```

wheel contract test 必须验证 `py.typed` 和 `.pyi` 被打包。增加一个最小 mypy fixture，覆盖 import、
attribute read、mapping read、nested record、failure narrowing 和错误字段拒绝。

### 5.4 `sdk.content.read_many()`

`read` 同步收缩为单 source 接口：

```python
sdk.content.read(
    source: str,
    *,
    offset: int = 1,
    limit: int = 200,
    max_chars: int = 100_000,
) -> Record
```

broker `content.read` 只接受单数 `source` 并直接返回一个 record；不再接受 `sources` 或返回 list。
多个 source 即使共享同一个 window，也统一通过 `read_many` 表达。

目标签名：

```python
sdk.content.read_many(
    windows: list[dict[str, Any]],
) -> list[Record]
```

每个 window 只接受以下字段，额外字段失败：

```python
{
    "source": str,                 # required
    "offset": int,                 # default 1
    "limit": int,                  # default 200
    "max_chars": int,              # default 100_000
}
```

返回值与 `windows` 一一对齐：

```python
{
    "input_index": int,
    "source": str,
    "text": str,
    "url": str | None,
    "title": str,
    "date": str | None,
    "failure": CapabilityFailure | None,
    "metadata": {
        "start_line": int,
        "end_line": int,
        "total_lines": int,
        "next_offset": int | None,
        # truncation fields remain conditional
    },
}
```

语义：

- 保持输入顺序和重复 window；
- 相同 source 的不同 window 只抓取一次 backend document，再分别切片；
- `content_fetches` 按 window 数记录策略行为，`content_backend_fetches` 按真实 fetch 记录；
- source admission、cache、dedup、deadline 和 failure promotion 与单 source `read` 一致；
- `read` 与 `read_many` 共用一个 window slicing helper；单 source `read` 不包含 `input_index`，
  `read_many` row 必须包含；
- 空输入返回 `[]`；window 数受 `max_sources_per_request` 限制；
- 不增加 `concurrency` 参数，broker 已拥有 content batch 并发与 provider gate。

典型用法：

```python
windows = [
    {
        "source": match.source,
        "offset": max(1, match.line - 5),
        "limit": 11,
    }
    for match in report.matches
]
rows = sdk.content.read_many(windows)
```

### 5.5 `sdk.content.grep()` 与参数验证

所有 integer 参数拒绝 `bool`、float、numeric string 和越界值。所有 string 参数拒绝非字符串，
不再通过 `str(...)` 自动转换。

公开范围：

| Operation | 参数 | 目标规则 |
| --- | --- | --- |
| `search` / `search.many` | query | `str`，trim 后非空，长度不超过动态 limit |
| `search` | `limit` | integer，`1..100` |
| `search` | `offset` | integer，`0..500`，且 depth 不超过 capability |
| `search.many` | `concurrency` | integer，`1..20` |
| `content.read*` | `offset` | integer，`>=1`，不再自动 clamp |
| `content.read*` | `limit` | integer，`1..5000` |
| `content.read*` | `max_chars` | integer，`1..400000` |
| `content.grep` | `context` | integer，`0..20` |
| `content.grep` | `max_matches_per_source` | integer，`1..200` |
| `content.passages` | `limit` | integer，`1..100` |
| `content.passages` | `max_per_source` | integer，`1..10` |
| `llm.complete*` | `temperature` | finite number，`0.0..2.0` |
| `llm.complete*` | `max_tokens` | integer 或 `None`，`1..32000` |
| `llm.complete_many` / `extract_many` | `concurrency` | integer，`1..12` |

SDK operation 从 `sdk.content.grep_report` 重命名为 `sdk.content.grep`，broker method 同步从
`content.grep_report` 重命名为 `content.grep`。不保留旧方法、旧 broker method 或兼容 shim。

目标签名：

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

目标返回形态：

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
            "line": int,          # 1-based；可直接作为 read/read_many offset
            "text": str,          # 整个 matched line，不是 occurrence fragment
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
            "match_count": int,   # report.matches 中属于该 input 的行数
            "scan_complete": bool,
            "failure": CapabilityFailure | None,
        }
    ],
    "input_count": int,
}
```

现有 match 字段全部保留，理由如下：

- flat `matches` 让常见的 `for match in report.matches` 保持低认知成本；
- `source` 可直接传给 content 方法，`title` 让独立打印 match 时仍可读；
- `line` 与 `read(offset=...)` 使用同一套 1-based coordinate；
- `before`/`after` 比单一 context list 更清楚地表达匹配行两侧；
- `input_index` 使重复 source 仍可区分。

不在本轮增加 character span 或把 match 按 source 嵌套。grep 的公共语义仍是“匹配行”，不是列出
一行内的每个 occurrence；需要精确 span 的程序可以在 `match.text` 上本地再次执行 regex。

`source_results` 取代顶层 `failures`：

- 数量必须等于 `input_count`，按输入顺序排列，`input_index` 必须是连续的 `0..input_count-1`；
- 成功零匹配表示 `match_count=0`、`scan_complete=True`、`failure=None`；
- fetch/admission failure 表示 `match_count=0`、`scan_complete=False`、`failure` 非空；
- 因 `max_matches_per_source` 在文档结束前停止时，`scan_complete=False`；
- 即使返回的 match 数刚好等于 limit，只要循环实际扫描到文档末尾，`scan_complete=True`；
- `match_count` 必须等于 flat `matches` 中相同 `input_index` 的数量；
- 有 failure 的 input 不得产生 match。

`pattern/mode/case_sensitive/context/max_matches_per_source` 回显实际生效值，使保存到 state 的 report
可以独立复现和解释。report 不增加冗余的全局 `match_count`，调用者可使用 `len(report.matches)`。
保留 `input_count` 作为请求规模和 contract validation 的显式锚点，并要求它始终等于
`len(report.source_results)`。

匹配规则：

- `mode="regex"` 时非法正则抛出 `ValueError`；
- `mode="literal"` 时对输入执行 literal search；
- 不保留“非法 regex 自动变 literal”的隐藏分支；
- `case_sensitive` 在两种 mode 下语义一致。

宿主使用 `ContentGrepReport` 对最终结果执行完整 validation 后再 `model_dump`。validator 必须检查
source result 对齐、match count、failure/match 互斥和 input index 关系，禁止 handler 手写未验证 dict
直接越过 wire contract。

### 5.6 逐项 failure

不引入通用 `Result`。目标规则：

1. 整次 RPC 失败仍抛 `BrokerError`。
2. 输入对齐的 recoverable item failure 使用单数字段 `failure`。
3. 排名型 report 无法逐输入对齐时继续使用 `failures: list[ContentFailure]`；grep 使用 flat
   `matches` 加输入对齐的 `source_results[*].failure`，不再保留顶层 `failures`。
4. `CapabilityFailure` 稳定包含全部字段；无值字段序列化为 `None`，不允许有时缺 key：

   ```python
   {
       "code": str,
       "message": str,
       "retryable": bool,
       "attempts": int,
       "provider_status": int | None,
       "retry_after_seconds": float | None,
   }
   ```

5. `llm.extract_many` 的 row 将 `error` 重命名为 `failure`：

   ```python
   {
       "index": int,
       "data": dict | None,
       "failure": OperationFailure | None,
       "attempts": int,
   }
   ```

6. `complete_many` 明确保留 all-or-nothing advanced 语义；本轮不改变为 row report。若未来需要
   partial completion，新增独立 report operation，不让 `list[str]` 在 patch release 中变形。
7. 零 search hit、零 grep match、零 passage 和空 batch 都继续是成功结果。

宿主必须对 grep/passages/extraction 的最终 report 执行 contract validation 后再 `model_dump`，避免
手写 fallback dict 绕过字段完整性。

### 5.7 `sdk.session.usage()` 对账

保留现有顶层字段，并增加：

```python
{
    # existing logical fields remain
    "content_backend_fetches": int,
    "pipeline_output_tokens_reserved": int,
    "sandbox_seconds": float,
    "workspace_bytes": int,
    "budget_consumed": {
        "max_exec_calls": int,
        "max_search_queries": int,
        "max_content_fetches": int,
        "max_pipeline_llm_calls": int,
        "max_pipeline_output_tokens": int,
        "max_sandbox_seconds": float,
        "max_workspace_bytes": int,
    },
    "provider": {
        "search_attempts": int,
        "content_attempts": int,
        "retries": int,
        "intra_call_deduplicated_items": int,
        "coalesced_requests": int,
        "queue_seconds": float,
        "rate_limit_wait_seconds": float,
        "backoff_seconds": float,
    },
}
```

`budget_consumed` 必须使用与 `budget_remaining` 完全相同的 `_BUDGET_USAGE_FIELDS` 映射构造，保证：

```text
bounded resource: consumed + remaining == configured limit
unbounded resource: remaining is None, consumed still reports actual value
```

## 6. 兼容性与 contract 版本

| 变更 | 兼容性 | Sandbox contract | Capability contract |
| --- | --- | --- | --- |
| 严格 JSON，移除 `default=str` | SDK-local breaking behavior | bump | 不单独 bump |
| `py.typed` 和 stubs | additive | 不单独 bump | 不 bump |
| 新增 `session.capabilities` | additive RPC surface | bump | bump |
| 新增 `content.read_many` | additive RPC surface | bump | bump |
| `content.read` 改为单 source/单 record | breaking request/response shape | bump | bump |
| 参数从 clamp/coerce 改为 reject | breaking request semantics | bump | bump |
| `grep_report -> grep`、显式 mode、重构 report | breaking RPC/request/response surface | bump | bump |
| extraction `error -> failure` | breaking response shape | bump | bump |
| usage 增加字段 | additive response shape | 同批 bump | 同批 bump |

本轮作为一个版本匹配的 0.6.4 contract 发布：

```text
OpenSAC host            0.6.3 -> 0.6.4
opensac_sdk             0.6.3 -> 0.6.4
SANDBOX_CONTRACT        9 -> 10
capability_contract     8 -> 9
```

虽然版本号使用 patch increment，本次发布包含明确的 SDK/wire breaking change，不能视为普通的
drop-in compatible patch。OpenSAC 采用版本匹配的 host 与 bundled sandbox SDK，并通过 sandbox/
capability contract 阻止不兼容组合；部署方必须成对升级 service 和 sandbox image。release note、
migration guide 和发布公告必须突出这一点。

`sandbox/Dockerfile` 的 contract label、runtime constant、API environment manifest、health payload、
测试 fixture 和 release assertion 必须在同一 release commit 中一致。

## 7. 实施顺序

阶段 1–6 是同一个 integration PR 内的 reviewable commit/workstream，不得以不完整 contract 状态
分别合入或发布。integration PR 必须一次性更新 host、SDK、sandbox、docs、Skills、contract numbers
和 tests，不能出现“SDK 已发请求但 broker 无 handler”、同一 contract number 对应不同 surface，
或默认 Skill 教授尚不可用方法的中间状态。

如果 review 流程必须使用 stacked PR，它们只能堆叠在未发布的 integration branch 上；最终 stack
完整且 Gate 通过后再合入目标 branch。

### 阶段 1：严格 JSON 与 type-only contracts

修改：

- 抽取 SDK-private strict JSON encoder；
- 迁移 state/output 写入方法，删除 `default=str`；
- 为 replace-style state 写入增加原子替换；
- 添加 `py.typed`、`__init__.pyi` 和 wheel/type fixture；
- 不改变包根运行时导出。

验收：

- Record、nested JSON、Unicode 正常 round-trip；
- datetime、set、NaN、Infinity、循环结构在文件写入前失败；
- 失败时原 artifact 不被覆盖；
- mypy 能识别 `hit.source`、`hit["source"]` 和 nested failure 字段；
- wheel 包含 marker 和 stub。

### 阶段 2：session capabilities 与 usage

修改：

- 从 API environment manifest 抽取共享 capability builder；
- 增加 broker `session.capabilities` handler 和 SDK method；
- 扩展 `session.usage`；
- 更新 surface manifest、broker method manifest、API schema 和 tracing policy；
- 验证返回中没有 secret 或内部 provider endpoint。

验收：

- local/web backend 的 `supports_domains` 与 `max_depth` 正确；
- direct URL admission 两种配置正确；
- LLM configured/unconfigured 正确；
- session mechanism override 正确；
- usage budget 可以逐字段对账。

### 阶段 3：统一参数验证与 `content.grep`

修改：

- 为 search/content/LLM public parameters 添加 SDK-local validator；
- broker 使用相同范围重复校验；
- 移除 `str(...)`、`int(...)` 隐式 coercion 和非文档化 clamp；
- 将 SDK `grep_report` 和 broker `content.grep_report` 重命名为 `grep` 和 `content.grep`，
  移除旧入口；
- 将 API environment feature 从 `content_grep_report_v1` 替换为 `content_grep_v2`；
- 增加 `mode`、`case_sensitive` 和有效参数回显；
- 用输入对齐的 `source_results` 取代顶层 `failures`，记录 `match_count`、`scan_complete` 和
  `failure`；
- 由 `ContentGrepReport` 校验 flat matches 与 source results 的跨字段关系；
- invalid request 必须在 provider charge、budget reserve 和 side effect 之前失败。

验收：

- 对每个参数覆盖 min、max、min-1、max+1、bool、float、string 和 `None`；
- SDK 与 broker 直接调用的接受范围一致；
- regex/literal × case-sensitive/insensitive 四种组合有测试；
- malformed regex 在 regex mode 失败，在 literal mode 正常匹配；
- `grep_report` 和 `content.grep_report` 不再出现在当前 SDK surface 或 broker method
  manifest；
- API environment 只声明 `content_grep_v2`，不再声明旧 grep feature；
- source success、零匹配、fetch failure、limit 截断和恰好在文档末尾达到 limit 均能准确表达；
- `source_results` 顺序、index、match count 和 failure/match 互斥关系通过 contract validation。

### 阶段 4：`content.read_many`

修改：

- 新增 host request/row contract；
- 将 SDK/broker `read` 改为单数 `source` 参数和单 record 返回值，拒绝旧 `sources`；
- 抽取 `read` 与 `read_many` 共用的 line-window helper；
- 实现同 source fetch dedup 和不同 window slicing；
- 添加 broker handler、SDK method、surface manifest 和 trace result sizing；
- 更新 usage 计数。

验收：

- 不同 source、offset、limit、max_chars 保持输入对齐；
- 重复 source 只产生一次 backend fetch；
- 同一 source 的重复/不同 window 都返回正确文本；
- admission failure、provider failure、全局 failure promotion 与单 source `read` 一致；
- `grep.line -> read_many.offset` 无算术偏差；
- 空输入与最大 batch 边界正确。

### 阶段 5：failure 归一化

修改：

- extraction row 改用 `failure`；
- 全部 capability failure 由 contract model dump 生成完整 key 集合；
- grep 的 `source_results` 及 passages report 在 wire 返回前验证；
- 更新 SDK docs、stubs、examples 和 tests；
- 保留 `complete_many` all-or-nothing 语义。

验收：

- 所有 recoverable item failure 有稳定 `code/message/retryable`；
- provider metadata 字段始终存在，未知时为 `None`；
- 逐项失败与整批失败边界有回归测试；
- 日志和 trace 不包含 provider 原始敏感响应。

### 阶段 6：agent guidance、文档与示例

修改：

- 更新中英文 SDK reference；
- 更新 README、examples、runtime `__doc__`；
- 更新 MCP/CLI Search-as-Code Skill exact signatures；
- 从当前文档、Skill 和示例中移除 `grep_report`，统一使用 `grep`；
- core workflow 增加 capability preflight 和 heterogeneous read 示例；
- 增加 0.6.4 migration guide 和 release notes；
- 历史 0.4/0.5/0.6 文档保持历史事实，不做全局替换。

验收：

- 文档自动审计覆盖全部 22 个 public operation；
- 中英文 section 顺序、签名和 tier 一致；
- Skill 中不存在 `grep_report`、旧 extraction `error` 或隐式 literal fallback；
- canonical examples 在 sandbox contract test 中真实执行。

### 阶段 7：0.6.4 release

只有前述 PR 全部合并且 CI 绿色后才执行：

- 同步更新 host/SDK version 为 `0.6.4`；
- 更新 Compose、环境示例和 sandbox image tag；
- 同步更新 sandbox/capability contract assertion；
- 执行 `docs/releasing.md` 的完整检查；
- 提交 release commit；
- 确认 tag 不存在后创建 annotated `v0.6.4` tag；
- 原子推送目标 branch 和 tag；
- 验证 GitHub Release、service image、sandbox image 和 `contract-10` channel。

## 8. 预计文件范围

### SDK

```text
packages/opensac-sdk/src/opensac_sdk/__init__.py
packages/opensac-sdk/src/opensac_sdk/__init__.pyi        # new
packages/opensac-sdk/src/opensac_sdk/py.typed            # new
packages/opensac-sdk/src/opensac_sdk/_record.py
packages/opensac-sdk/src/opensac_sdk/_resources.py
packages/opensac-sdk/src/opensac_sdk/_surface.py
packages/opensac-sdk/src/opensac_sdk/transport.py
packages/opensac-sdk/pyproject.toml
```

### Host 与 broker

```text
src/opensac/_contracts.py
src/opensac/models.py
src/opensac/api/runtime.py
src/opensac/api/sessions.py
src/opensac/broker/service.py
src/opensac/broker/capabilities/search.py
src/opensac/broker/capabilities/content.py
src/opensac/broker/capabilities/llm.py
src/opensac/broker/validation.py
src/opensac/sandbox/docker_core.py
sandbox/Dockerfile
```

### Tests、Skills 与文档

```text
tests/test_sdk.py
tests/test_broker.py
tests/test_broker_provider.py
tests/test_api.py
tests/test_search_as_code_skill.py
tests/test_sac_agent.py
tests/test_sandbox_docker_e2e.py
tests/typecheck/sdk_contract.py                         # new
examples/
.agents/skills/search-as-code/
docs/sdk-reference.md
docs/sdk-reference.zh-CN.md
README.md
```

实际实现前必须用 `rg` 定位所有 capability contract、sandbox contract、旧字段名和 exact signature
fixture；不得依赖本清单猜测不存在的路径。

## 9. 测试计划

### 9.1 Unit tests

- strict JSON 的成功、拒绝和原文件保护；
- 所有 SDK-local 参数 validator；
- Record wrapping 与 type-facing shape；
- RRF/state 现有 helper 不回归；
- capabilities/usage exact fields；
- read/read_many 共用坐标与 truncation；
- grep mode、有效参数回显、scan completeness 和 source/match 关系；
- failure complete-key contract。

### 9.2 Broker integration tests

- local 与 web backend capability 差异；
- LLM configured/unconfigured；
- batching/persistence/LLM mechanism ablation；
- invalid request 零 charge、零 provider attempt；
- read_many dedup、cache、failure promotion、deadline；
- grep success、零匹配、fetch failure、cap truncation 和 document-end completeness；
- extraction partial failure 和 repair；
- usage/budget reconciliation。

### 9.3 Contract tests

- `SDK_SURFACE` 与 resource method 一一对应；
- SDK broker method 与 `CAPABILITY_METHODS` 一致；
- capability contract 为 9；
- public operation 22 个，model core 11 个；
- root `__all__` 不变；
- stubs、runtime、reference docs 和 Skills 的签名一致；
- host contract 的字段集合与 SDK type stub 一致；
- 当前 surface 和 broker manifest 不包含旧的 `grep_report` method。

### 9.4 End-to-end

真实 sandbox 程序必须完成：

1. 读取 `sdk.session.capabilities()`；
2. 根据 `supports_domains` 决定是否传 domain filter；
3. search + grep；
4. 用 `read_many` 展开不同 match window；
5. 写入并恢复严格 JSON state；
6. 读取可对账的 usage；
7. 提交最终 output；
8. 验证 host/SDK `0.6.4`、sandbox contract 10、capability contract 9。

## 10. 调用者迁移

### JSON value

- datetime/date 显式 `.isoformat()`；
- `Path` 显式 `str(path)`；
- set 显式排序或转换为 list；
- NaN/Infinity 转换为 `None` 或领域内明确的字符串/状态。

### 参数范围

- 删除依赖 silent clamp 的调用；
- 计算 read 上下文时显式 `max(1, line - context)`；
- 不再向 typed string/int 参数传可隐式转换的对象。

### Grep

```python
# 0.6.3
sdk.content.grep_report(sources, r"born in \d{4}")

# 0.6.4：正则
report = sdk.content.grep(sources, r"born in \d{4}", mode="regex")

# 0.6.4：字面文本
report = sdk.content.grep(sources, "C++ (programming)", mode="literal")

# 区分完整零匹配、扫描截断和逐项失败
for source_result in report.source_results:
    if source_result.failure is not None:
        print(source_result.source, source_result.failure.code)
    elif not source_result.scan_complete:
        print(source_result.source, "results capped", source_result.match_count)
    elif source_result.match_count == 0:
        print(source_result.source, "no matches")
```

### Extraction failure

```python
# 0.6
if row.error is not None:
    print(row.error.code)

# 0.6.4
if row.failure is not None:
    print(row.failure.code)
```

### Heterogeneous read

```python
# 0.6.3
row = sdk.content.read([source], offset=20, limit=10)[0]

# 0.6.4：单 source
row = sdk.content.read(source, offset=20, limit=10)

# 0.6.4：多个 source 或不同 window
rows = sdk.content.read_many(windows)
```

## 11. 风险与控制

| 风险 | 控制 |
| --- | --- |
| capabilities payload 过大，占用程序输出预算 | 只暴露决策所需字段，不返回 provider 实现细节 |
| host environment 与 SDK capabilities 漂移 | 使用一个共享 builder，加 exact equality contract test |
| stub 与 runtime shape 漂移 | repository test 对照 host model 字段和 method signature |
| strict JSON 破坏依赖隐式字符串化的程序 | 0.6.4 migration guide、明确 ValueError、release note 列出示例 |
| read_many 增加一次性 response 体积 | 复用 source/window limits、max_chars 和 broker trace redaction |
| 参数 reject 增加旧程序失败率 | 在 SDK 本地给出包含字段、值和公开范围的可操作错误 |
| grep report 增加输入对齐状态，响应体变大 | 每个 input 只增加一个小型 summary，不复制 match 内容 |
| failure rename 造成隐蔽 AttributeError | 版本和 contract bump；Skills/examples/fixtures 同批迁移 |
| model core 变大 | 保持 11 个 operation，不把 type/state advanced 内容放入首屏 |

若实施中发现 capability payload 或 read_many response 无法在现有 observation budget 内稳定运行，应
回退该 operation 的字段规模或默认 batch limit，而不是绕过 trace/output 安全上限。

## 12. 验收标准

1. SDK public operation 恰好 22 个，model core 恰好 11 个。
2. 包根仍只导出 `BrokerError`、`sdk` 和 `__version__`。
3. SDK wheel 包含 `py.typed` 和 type stub，最小 mypy fixture 通过。
4. state/output 不再包含 `default=str`，所有正式 artifact 拒绝非标准 JSON。
5. `session.capabilities()` 与当前 backend、LLM 配置、session mechanisms 和公开 limits 一致。
6. SDK 程序无需用失败试探 domain filter、direct URL 或 LLM availability。
7. 所有公开参数在 SDK/broker 两层使用相同类型和范围；不存在未文档化 clamp/coercion。
8. SDK/broker 只暴露 `grep`；regex/literal 和 case sensitivity 显式可测，返回值能区分零匹配、
   逐项失败与未完成扫描，并通过跨字段 contract validation。
9. `read` 只接受单 source 并返回单 record；`read_many` 保持输入对齐、同源 fetch dedup、正确
   line coordinate 和逐项 failure。
10. recoverable item failure 使用稳定字段，extraction 使用 `failure` 而不是 `error`。
11. usage 中每个有界 budget 都满足 `consumed + remaining == limit`。
12. 中英文 reference、runtime doc、Skills、examples 和实际 surface 无漂移。
13. `SANDBOX_CONTRACT=10` 与 Docker label、runtime、fixture 一致。
14. `capability_contract=9` 与 API manifest、broker methods、SDK 和 tests 一致。
15. focused、full、Docker E2E、build 和 release metadata checks 全部通过。

## 13. 发布 Gate

每个实现 PR 至少运行最快相关检查；release 前运行完整集合：

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest tests/test_sdk.py
uv run pytest tests/test_broker.py tests/test_broker_provider.py
uv run pytest tests/test_api.py
uv run pytest tests/test_search_as_code_skill.py tests/test_sac_agent.py
uv run mypy tests/typecheck/sdk_contract.py
uv run pytest
OPENSAC_DOCKER_E2E=1 uv run pytest tests/test_sandbox_docker_e2e.py
uv build --all-packages --out-dir dist --clear
uvx --from twine twine check dist/*
uv run python scripts/release.py --tag v0.6.4
```

发布 commit 必须包含详细 body，说明动机、重要变更、验证命令、contract 变化和安全影响。只有该
commit 已位于目标 release branch、tag 尚不存在且所有 release Gate 通过后，才能创建 annotated
`v0.6.4` tag 并原子推送 branch 与 tag。
