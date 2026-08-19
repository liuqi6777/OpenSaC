# OpenSAC 公共接口收敛实现设计

- 状态：实施中
- 目标版本：OpenSAC 0.6
- 范围：MCP、sandbox SDK、Python host client 与对应契约测试

## 1. 背景

OpenSAC 的外部模型接口设计为一个操作：`sac_run(code)`。模型在程序内通过
`opensac_sdk` 组合搜索、正文读取、状态、pipeline LLM 和引用能力。随着 0.2—0.5
逐步加入可靠性、typed partial failure、持久状态和 passage retrieval，底层能力仍然保持了
broker 管理边界，但 SDK facade 和公共类型持续累积。

当前基线为：

| 层级 | 当前规模 | 主要问题 |
| --- | ---: | --- |
| MCP discovery | 2 个工具 | `bind_context` 标记为内部，但仍可被客户端发现 |
| Broker capability | 13 个 RPC method | 数量可控，但只有方法名 manifest，没有参数级防漂移 |
| Sandbox SDK | 7 个 namespace、约 24 个操作 | core、helper、advanced 与旧接口没有分层 |
| SDK 顶层导出 | 26 个符号，其中 22 个模型类型 | 实现类型被提升为默认公共入口 |
| Host client | 同步/异步各 8 个方法 | 普通执行、session 生命周期和 admin 操作混在一个类中 |

接口增长本身不是错误。以下能力有明确保留理由：

- `search.fuse_rrf` 是稳定、确定且保留 provenance 的本地组合；
- `state.merge_jsonl` 来自多轮研究中候选池碎片化的实测失败；
- typed result/failure 对 batch 对齐和恢复策略是必要的；
- provider、trace、预算、worker 和 sandbox 的内部复杂度不应为了缩短公共 API 而删除。

本重构解决的是公共契约缺少层级、旧接口持续占用实现面和文档无法机械同步的问题。

## 2. 目标与非目标

### 2.1 目标

1. 模型默认只学习完成常规 Search-as-Code 工作流所需的 core surface。
2. 每个保留的 SDK 操作明确归类为 `core`、`helper`、`advanced` 或 `internal`。
3. `opensac_sdk` 顶层只保留 `sdk`、`BrokerError` 和版本信息。
4. 结果保持 typed，但 supporting model 不再全部出现在顶层 namespace。
5. SDK、broker、Skill、README 和 MCP schema 由 contract test 检查，避免手工漂移。
6. 0.6 直接删除已有 canonical 替代的旧接口，不在运行时保留兼容 wrapper。
7. 每个阶段形成可独立审查、可独立回滚的小 PR。

### 2.2 非目标

- 不重写 broker dispatch、provider runtime、sandbox 或 evidence registry。
- 不把全部操作合并成一个高层 `research()` 黑盒。
- 不删除为 batch alignment、failure recovery 或 provenance 服务的类型。
- 不在本重构中更换搜索、抓取、passage ranker 或 pipeline model provider。
- 不同时改变资源预算和 trace 计量语义。

### 2.3 简洁性、优雅性与可扩展性约束

公共接口收敛必须同时降低实现复杂度。不能只把方法从文档中隐藏，却继续在内部叠加 wrapper、兼容分支和重复模型。

实现遵循以下规则：

| 规则 | 工程约束 |
| --- | --- |
| 组合优先 | 新需求先用现有原语和普通 Python 组合；新增方法必须说明现有组合为什么不足 |
| 单一权威表示 | 一个概念只保留一个字段、错误表示和序列化路径 |
| 单向委托 | singular/batch 与 sync/async facade 必须委托同一实现，不能复制业务逻辑 |
| 不做推测性抽象 | 只有出现两个独立实现、明确替换点或真实测试痛点时才增加 Protocol、基类或 registry |
| 边界稳定、内部可变 | 稳定 Pydantic/HTTP/SDK contract；内部使用小函数和值对象，不把实现类升级成公共类型 |
| breaking change 完整 | 不保留 deprecated wrapper；通过版本与 contract bump 明确拒绝旧调用 |
| 删除完整 | 删除接口时同步删除 handler、wrapper、类型、测试 fixture 和文档，不留下无调用代码 |
| 局部可推理 | 一个模块只承担一组共同变化的职责；跨域依赖通过显式参数，不读取隐式全局状态 |

复杂度预算：

- `opensac_sdk.__all__` 固定为 3 个符号；
- `MODEL_CORE_METHODS` 上限为 12，超过时必须先完成独立设计审查；
- 同一 payload、feature negotiation 或 failure mapping 只能有一个实现；
- 不允许在 broker 主流程中按具体 provider 名称增加条件分支，扩展通过现有 backend/reranker Protocol；
- 超过 1,000 行且包含多个独立变化原因的模块必须触发职责审查；行数是信号，不是机械拆文件指标；
- 新增一个公共操作时，PR 必须列出新增的类型、分支、测试矩阵和将来删除成本。

新增公共操作的设计说明必须回答：

1. 它是否需要网络、密钥、索引或 broker session state？
2. 普通 Python 或现有方法为什么不能清楚表达？
3. 单个与 batch、成功与 partial failure 的语义是否统一？
4. 它是否引入新的结果类型；能否复用已有语义类型而不增加 optional 字段？
5. 它属于 core、helper 还是 advanced；是否替代并最终删除现有方法？
6. 哪个真实 benchmark、trace 或故障样例证明它值得进入长期维护面？

## 3. 公共接口分层

### 3.1 分层定义

| Tier | 含义 | 默认进入 Skill | 兼容承诺 |
| --- | --- | --- | --- |
| `core` | 常规研究程序完成任务所需的最小接口 | 是 | 同一 major contract 内稳定 |
| `helper` | 无额外权限的本地确定性组合或状态辅助 | 按 workflow 渐进披露 | 语义稳定，但不属于 broker primitive |
| `advanced` | 特殊任务或调试需要的 escape hatch | 否 | 有文档和测试；变更需 migration note |
| `internal` | transport、RPC、resource construction 和 host 控制面 | 否 | 无公共兼容承诺 |

Tier 描述的是暴露策略，不等于代码目录。`helper` 可以继续作为 `sdk.search` 或 `sdk.state` 的方法，
但不能被计入 broker capability，也不应与 core 方法并列出现在 README 的第一屏。

### 3.2 三套契约

重构后必须区分三套集合：

```text
MODEL_CORE_METHODS     模型默认学习的方法
SDK_PUBLIC_OPERATIONS  core + helper + advanced
CAPABILITY_METHODS     需要 broker 权限、预算和 trace 的 RPC 方法
```

三者不能再用“SDK surface”笼统指代。`search.fuse_rrf` 和 `state.*` 可以是 SDK public operation，
但不是 broker capability；来自 MCP request metadata 的 host binding 也不属于 model surface。

## 4. 目标 SDK 契约

### 4.1 顶层导入

生成程序的标准导入保持为：

```python
from opensac_sdk import BrokerError, sdk
```

目标顶层导出：

```python
__all__ = ["BrokerError", "sdk", "__version__"]
```

需要显式类型注解的宿主或高级程序从 `opensac_sdk.types` 导入语义模型。transport 和 wire 类型不进入
`types`。`OpenSACClient` 从 `opensac_sdk.client` 导入，不再由包顶层重导出。

`opensac_sdk.types` 只负责组织稳定类型路径，不要求合并现有 Pydantic 模型。不能为了减少类型数量构造
包含大量 optional 字段的通用 `Result`，否则会丢失当前的校验和可恢复失败语义。

### 4.2 操作分层

| Namespace | Operation | Tier | 0.6 处理 |
| --- | --- | --- | --- |
| `search` | `__call__` | core | 保留；单 query 的低认知成本入口 |
| `search` | `many` | core | 保留；主要 batch retrieval 入口 |
| `search` | `fuse_rrf` | helper | 保留；明确标注为本地方法、不产生 RPC |
| `content` | `passages` | core | 保留；语义证据发现的主要入口 |
| `content` | `read` | core | 保留；精确上下文扩展 |
| `content` | `grep_report` | core | 保留；零匹配与抓取失败可区分 |
| `content` | `get_many` | advanced | 保留；整页读取 escape hatch |
| `citations` | `resolve` | advanced | 保留；triage 和调试使用 |
| `citations` | `resolve_requests` | advanced | 评估与 `resolve` 合并，0.6 不直接删除 |
| `session` | `usage` | core | 保留；程序据此改变继续或停止策略 |
| `llm` | `extract_many` | core/optional | pipeline model 可用时进入 core profile |
| `llm` | `complete` | advanced | 保留；自由文本子程序 |
| `llm` | `complete_many` | advanced | 保留；自由文本 fan-out |
| `state` | JSON/JSONL serde | helper | 保留；显式跨 execution 状态 |
| `state` | `merge_jsonl` | helper | 保留；有真实 rollout 失败依据 |
| `state` | `append_jsonl` | helper | 保留；追加写入不要求 read-rewrite |
| `state` | `exists`、`list` | helper | 保留；恢复与 namespace 发现 |
| `output` | `submit` | core | 保留；唯一正式结果与 citation 提交入口 |

0.6 删除 `snippets` 与 `grep`；它们不再出现在 SDK surface、broker capability、Skill 或测试 fixture 中。
`resolve_requests` 仍有独立输入语义，因此作为 advanced operation 保留。

### 4.3 Core profile

标准 Skill 第一层只教授：

```text
search -> search.many -> search.fuse_rrf
content.passages -> content.read / content.grep_report
llm.extract_many（仅在 capability manifest 声明可用时）
session.usage
output.submit
```

`state` 在单次程序不需要跨 execution 时不进入首屏；多轮任务再加载 state reference。advanced 方法只在
按需 reference 中出现。

## 5. 类型与失败语义

### 5.1 类型路径

实施时新增 `packages/opensac-sdk/src/opensac_sdk/types.py`，只重导出语义结果类型。以下内容继续留在
`models.py`，但不属于公共类型入口：

- `RpcRequest`、`RpcResponse`、`RpcError`；
- `SubmittedOutput`；
- 仅用于 transport 或反序列化的 supporting type。

具体结果对象仍可包含嵌套的 supporting model。用户不需要为了读取字段而导入这些类型；需要静态注解时再从
`opensac_sdk.types` 显式导入。

### 5.2 失败规则

重构不得改变以下语义：

1. 整个 RPC 失败抛出 `BrokerError`。
2. batch 中单项失败返回 typed item failure，并保持输入对齐。
3. 空 search hits、零 grep matches 和零 passages 是成功结果。
4. core content 方法不得隐藏 partial fetch failure。
5. locator 缺失不能被自动降级成正文 citation。

不引入统一 `Result[T]` envelope。现有 search、content、extraction 的成功数据和恢复信息不同，强行统一会把
明确的类型变成大量 optional 字段。

## 6. MCP 暴露面

### 6.1 目标

对支持 conversation identity metadata 的宿主，`list_tools()` 必须只返回：

```text
sac_run
```

Codex 已可从 MCP request metadata 解析 task identity，应直接满足该目标。

### 6.2 Claude Code 通道

Claude Code hook 可以读取 `session_id`，但 MCP tool hook 只能调用已注册、可 discovery 的 server tool；
当前 MCP request metadata 没有等价的 conversation identity。0.6 不为此保留第二个协议工具，也不使用进程级
固定 context、工作目录或“最近一次绑定”等会破坏并发隔离的回退。

因此 MCP adapter 只支持能提供 request metadata 的 Codex。Claude Code 使用已有 `agent-run` CLI adapter，
由 `CLAUDE_CODE_SESSION_ID` 在 host 侧完成绑定。未来只有出现标准、模型不可伪造的 metadata 通道时才重新增加
Claude Code MCP 支持，且不得增加第二个 model-controlled tool。

## 7. Host Python client 分层

Host client 在 SDK surface 稳定后单独重构，避免一次 release 同时改变两套调用者。

目标职责：

```text
OpenSAC
  create_session
  exec_code
  delete_session
  heartbeat_session
  abort_session

Admin REST control plane
  GET /healthz
  POST /v1/admin/drain
```

同步和异步 client 必须共享请求构造、feature negotiation 和错误映射逻辑。管理操作保持显式 REST 控制面，
不再为两个低频 endpoint 维护额外同步/异步 facade。

本阶段不要求把所有 `dict[str, Any]` 立即改成 Pydantic 返回值。先确定稳定的 host contract，再决定是否只为
`PublicSession` 和 `ExecResult` 提供两个根 DTO；不得把 `src/opensac/models.py` 的内部持久化类型全部导出。

## 8. 内部实现结构

公共 surface 的复杂度已经在内部形成两个明显集中点：

| 模块 | 当前规模 | 结构问题 |
| --- | ---: | --- |
| `src/opensac/broker/service.py` | 约 3,947 行 | session、dispatch、search、content、evidence、LLM 与 trace 同时变化 |
| `src/opensac/api/app.py` | 约 1,356 行 | runtime 生命周期、HTTP 装配、route 和错误映射混合 |

拆分目标不是追求小文件，而是让一次能力变更只需要进入一个局部上下文。

### 8.1 Broker 边界

保留 `BrokerService` 作为薄 facade、session registry 和 handler assembly。按共同变化原因提取以下内聚模块：

```text
broker/session.py     session state、in-flight execution 与缓存所有权
broker/search.py      query/query_many、identity、dedupe 与 search trace
broker/content.py     fetch、read、grep 与 passages orchestration
broker/passages.py    纯分段、BM25、预筛与稳定选择函数
broker/evidence.py    locator registry、验证与 citation resolution
broker/llm.py         complete/extract、schema validation 与 model usage
broker/trace.py       capability event 构造和无正文审计记录
```

以上是职责边界，不要求“一类一个文件”。只有拥有独立状态或生命周期的职责才使用 class；纯校验、转换和排序优先使用
小的 module function。`BrokerSession` 和共享值对象位于低层模块，业务模块不能反向导入 `BrokerService`。

拆分必须满足：

- handler table 仍有一个装配点；
- provider runtime、policy 和 capacity gate 通过构造参数显式注入；
- search、content、LLM 之间不互相调用私有方法；共享行为下沉为窄函数；
- 每移动一个 capability family，先保留 characterization test，再删除原位置实现；
- 不在结构迁移 PR 中改变 RPC、计费、retry、trace 或 failure 语义。

### 8.2 API 边界

`src/opensac/api/app.py` 最终只负责 FastAPI 装配和 lifespan。建议职责：

```text
api/runtime.py          ApplicationRuntime 与 session/exec 生命周期
api/errors.py           稳定 HTTP contract error 映射
api/routes/sessions.py  create/read/heartbeat/delete/abort/workspace
api/routes/executions.py
api/routes/admin.py     health/drain
api/app.py              settings、dependency wiring、router assembly
```

route 模块不能直接操作 store、sandbox 或 broker 私有状态，只调用 runtime 的窄方法。HTTP response model 继续是显式
contract；持久化 record 和 runtime-only model 不因拆文件而进入公共 API。

### 8.3 扩展点约束

OpenSAC 只保留已经存在真实变化轴的扩展点：search backend、passage reranker、sandbox 和 provider runtime。
新增 backend 或 reranker 应实现现有 Protocol 并在装配层注册，不修改核心检索流程。新增 capability 不自动意味着新增
抽象基类；在出现第二个实现之前，优先使用具体代码和窄函数。

禁止引入通用 plugin framework、依赖注入容器或事件总线来解决当前静态装配可以清楚表达的问题。

## 9. 实施拆分

### PR 1：Surface manifest 与防漂移测试

修改：

- 新增 `packages/opensac-sdk/src/opensac_sdk/_surface.py`；
- 更新 `src/opensac/models.py` 中 capability manifest 的校验；
- 更新 `tests/test_sdk.py`、`tests/test_broker.py` 和 Skill contract tests；
- 修正 README 的 LLM 方法名、`grep_report` 和 citations 描述。

`_surface.py` 是内部声明，不进入 `__all__`。每条记录至少包含：

```text
public_name, tier, transport_method, model_core
```

验收：

- 每个 resource 公共方法恰好对应一条 surface record；
- 每个 broker handler 恰好对应一个 `CAPABILITY_METHODS` 条目；
- core Skill 中出现的方法必须存在且 tier 为 core/helper；
- manifest 不允许声明 SDK 中不存在的方法。

### PR 2：顶层导出与类型路径

修改：

- 更新 `packages/opensac-sdk/src/opensac_sdk/__init__.py`；
- 新增 `packages/opensac-sdk/src/opensac_sdk/types.py`；
- 更新 examples、Skill、SDK tests 和 Docker contract tests；
- sandbox contract 递增一次；本 PR 不改变 wire method，因此不提升 capability contract。

迁移：

```python
# 0.5
from opensac_sdk import SearchHit

# 0.6
from opensac_sdk.types import SearchHit
```

SDK 与 sandbox 镜像版本匹配，因此 0.6 采用明确 breaking migration，不在顶层长期保留隐式兼容别名。

### PR 3：Content 收敛

修改：

- 更新 `content.py` docstring 与 API reference；
- canonical examples 全部迁移到 `passages`、`read`、`grep_report`；
- 删除 `snippets`、`grep` SDK 方法及 broker handler；
- 删除只服务旧接口的 passage selector、测试和 fixture；
- capability contract 从 4 提升到 5。

迁移：

```python
# 0.5
matches = sdk.content.grep(refs, pattern)

# 0.6 canonical
report = sdk.content.grep_report(refs, pattern)
matches = report.matches
failures = report.failures
```

`snippets` 不提供兼容 shim。调用者必须选择全局 `passages` 的候选 ref、limit 和 `max_per_ref`。

### PR 4：Skill 渐进披露

修改：

- 缩短 `.agents/skills/search-as-code/SKILL.md` 的方法枚举；
- 将 core/helper exact signature 与 advanced reference 分开；
- 同步 CLI Skill reference；
- contract test 执行 core pattern，而不只搜索方法名。

主 Skill 负责决策边界和 core workflow，SDK reference 负责精确签名，stateful reference 只在多轮任务读取。

### PR 5：MCP binding

- 从 `create_server()` 注销 `bind_context`；
- 删除 Claude MCP 的可变进程级 context；
- 增加精确单工具 discovery 断言；
- 保留 context resolver 和 generation registry；
- Codex 保持 MCP，Claude Code 明确使用 CLI adapter。

### PR 6：Broker 与 API 职责拆分

修改：

- 先为现有 capability family 增加 characterization tests；
- 先提取无 session/provider 副作用的 passage 纯函数；
- 按 search、content、evidence、LLM 的顺序从 `broker/service.py` 提取；
- 将 `ApplicationRuntime` 和 route 从 `api/app.py` 分离；
- 每个 PR 只移动一个职责，不同时重命名公共方法或改变行为；
- 移动完成后立即删除旧私有方法和重复 fixture。

验收：

- `BrokerService` 只负责编排、session ownership 和 handler assembly；
- `api/app.py` 只负责应用装配；
- 新模块之间没有循环导入；
- capability、usage、trace、retry 和错误 contract 的既有测试不变；
- 没有新增仅转发参数、但不提供边界价值的 class。

### PR 7：Host client 分层

修改：

- 从默认 client 删除 admin 方法，管理端直接使用 REST 控制面；
- 抽取同步/异步共享的 payload 和 feature 规则；
- 更新 `tests/test_client.py`、API 文档和 examples；
- 保持 HTTP route 不变，不在默认 client 上保留 admin 转发别名。

### PR 8：0.6 发布

0.6 发布新的顶层导入路径、收敛后的 surface 与 canonical Skill。release note 必须明确列出删除的
`content.snippets`、`content.grep` 与顶层类型导入，不用模糊的“API cleanup”概括 breaking change。

## 10. Breaking change 与版本策略

以下变更要求 sandbox contract bump：

- bundled SDK 顶层导入路径变化；
- 删除或重命名 sandbox 可调用方法，包括不经过 RPC 的 helper；
- SDK 类型、参数或结果字段发生不兼容变化。

以下变更同时要求 capability contract bump：

- 增加、删除或重命名 broker RPC method；
- broker method 的参数、结果或 failure 语义发生不兼容变化。

以下变更不要求 broker capability bump，但仍需 SDK 版本和测试：

- 本地 helper 的文档 tier 变化；
- `__all__` 收窄但运行时方法不变；
- Skill progressive disclosure 调整。

Service 和 sandbox 必须保持 version-matched。旧 image 不得在运行时静默接受新 Skill 承诺的方法。

## 11. 验证与发布 Gate

### 11.1 静态与单元测试

每个 PR 运行最快相关检查：

```bash
uv run pytest tests/test_sdk.py tests/test_search_as_code_skill.py
uv run pytest tests/test_search_as_code_cli_skill.py
uv run pytest tests/test_mcp_server.py tests/test_sac_agent.py
uv run pytest tests/test_client.py tests/test_api.py
uv run ruff check .
uv run ruff format --check .
```

发布前运行：

```bash
uv run pytest
uv run pytest tests/test_sandbox_docker_e2e.py
```

### 11.2 删除扫描

删除旧接口时扫描以下范围：

- `examples/`、`tests/data/`、`.agents/skills/`、`docs/`；
- 已保存的 rollout program archive；
- 项目内已知 external harness 和发布示例。

仓库内调用必须清零；已知 external harness 通过 0.6 release note 获知 breaking change，不在运行时增加兼容层。

### 11.3 Paired evaluation

用相同模型、题集、搜索后端、预算和 sampling 对比重构前后：

- program compile/validation success rate；
- capability failure 与 repair turn 数；
- search/content 调用量；
- citation locator 有效率；
- 任务主指标；
- p50/p95 wall time 和 pipeline model token。

接口收窄不得以明显增加 repair turn 或降低 citation 有效率为代价。`merge_jsonl` 等基于真实失败加入的 helper
只有在 paired result 证明替代方案不回退时才能删除。

### 11.4 可维护性检查

每个实施 PR 在功能测试之外回答：

- 是否减少了公共符号、重复实现、跨模块私有调用或历史分支？
- 新增的 class/Protocol/registry 是否已有两个真实消费者或独立实现？
- 修改一个 capability 是否只需要进入一个领域模块？
- 删除路径是否同时删除了代码、类型、测试和文档？
- 是否可以从函数签名和局部类型理解数据流，而不追踪隐式全局状态？

代码行数不作为单独验收指标；如果拆分后总行数增长，PR 必须说明增长来自必要的 contract test、显式错误处理还是
重复 wrapper。无法解释的结构性增长视为未完成。

## 12. 回滚策略

- PR 1—4 主要改变声明、导出和默认文档，可逐 PR revert。
- PR 6 按 capability family 独立迁移，可逐模块 revert，不使用跨全仓库的一次性重写。
- Content 删除可通过完整 revert 对应提交回滚，不在新代码上临时叠加 wrapper。
- 类型路径迁移若需回滚，同样 revert 顶层导出提交，不增加双路径重导出。
- MCP binding 删除可整提交回滚；不以进程共享 session 恢复 Claude Code MCP。
- Host client 分层不改变 HTTP route，调用者可回退到上一版 client。

## 13. 完成标准

本重构仅在以下条件全部满足时完成：

1. `opensac_sdk.__all__` 只包含 `BrokerError`、`sdk` 和 `__version__`。
2. 每个 SDK operation 有唯一 tier，core/helper/advanced 集合有 contract test。
3. README、Skill 与 SDK 不再出现不存在的方法名。
4. 常规 research workflow 不需要从包顶层导入结果模型。
5. core content workflow 不隐藏 partial fetch failure。
6. broker capability、SDK helper 和 host control operation 在文档中明确区分。
7. MCP `list_tools()` 精确返回 `sac_run`，缺少 Codex metadata 时 fail closed。
8. 已删除方法在 SDK、broker、Skill、examples、测试和非历史文档中的引用清零。
9. host admin 操作不再与常规执行方法共用同一个默认 facade。
10. migration note 提供所有 breaking import 和方法替代示例。
11. `BrokerService` 和 `api/app.py` 只保留装配与生命周期职责，领域逻辑可以独立测试。
12. sync/async 与 singular/batch 之间不存在复制的业务实现。
13. 新扩展通过既有 Protocol 或窄装配点完成，不需要在核心流程增加 provider-specific 分支。
14. 所有删除路径均移除对应 dead code、重复类型、fixture 和文档。
