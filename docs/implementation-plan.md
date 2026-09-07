# OpenSaC 实现计划

状态：当前交付范围为本地 Python 包。已移除 OpenSaC server、remote 模式、服务端鉴权、
HTTP envelope、Docker 部署及相关依赖。Provider 仍可访问远端搜索、抓取和模型服务。

## 产品与仓库边界

- 一个 `opensac` distribution，使用 `from opensac import sdk`。
- 调用方管理 Python 执行、变量、文件和研究产物；不管理执行 session 或逐任务容器。
- 文档只使用 URL，包括本地服务 URL；没有文档 ID、登记或先搜索后抓取的约束。
- Provider 按 search、fetch、rerank、llm 独立注册；实现文件按服务或协议命名。
- MCP 放弃，CLI 下一阶段，RL 最后阶段；不实现 agent-run 或远端代码执行。
- 独立开发仓库为 OpenSaC-next，最终公开身份仍为 `liuqi6777/OpenSaC`。
  当前不执行公开仓库迁移或发行。

## 当前结构与执行方式

```text
src/opensac/
  __init__.py           # sdk、Client、结果和错误类型
  client.py            # 同步 Python API
  bridge.py            # 本地同步／异步桥接
  runtime.py           # 能力执行与编排
  contracts.py
  config.py
  errors.py
  structured.py
  compose.py
  provider.py
  providers/
    base.py
    serper.py
    jina.py
    http.py
    openai.py
```

同步 SDK 惰性启动一个后台事件循环线程，复用异步 Provider 资源，并可在 Notebook 使用。
SDK 直接调用 Runtime 方法，参数在入口校验；不经过请求字典、Operation 或请求类型分发。
请求和批量输入直接使用方法参数；模型及生成参数由宿主通过 Provider 配置，Agent 只传任务内容。原生异步调用使用 Runtime。关闭前等待在途调用结束。Provider 凭据在调用方环境配置。
Schema 仅支持有大小、深度和节点限制的简单子集，在当前进程校验，不启动子进程。

## 已完成

- search、同一意图的多 query 融合、fetch、rerank、LLM complete/extract 和批量调用。
- 惰性 Provider 发现、生命周期、结构化错误、并发、超时和响应大小限制。
- `rerank` 返回原对象；搜索结果无 rank 字段，排序由列表顺序表达。
- 本地加权 RRF 融合、按 URL 去重；agent 指引维护在独立的 `skills/search-as-code/` 中。
- 单包构建和隔离 Python 3.12 安装验证，使用独立安装的第三方 Provider 及受控 HTTP 后端。

验收命令：pytest、Ruff、mypy、uv build、scripts/verify_wheels.py。
验证不使用付费 API；不将受控后端测试描述为真实 Provider 线上验证。

## 后续阶段

1. **能力型 CLI**：通过 `uv tool` 安装 `opensac search "query"`、`opensac fetch "url"`。
   复用 SDK 配置和实现，只处理参数、输出和退出码；处理旧命令安装冲突。
2. **按需完善缓存与用量**：有界 TTL、相同请求合并、累计计量。缓存按能力、配置、参数
   和权限范围组织；不与执行 session 绑定。不默认重放可能重复计费或结果未知的请求。
3. **公开发布**：发布 wheel/sdist，明确新旧版本衔接、安装迁移与兼容范围，再执行发布检查。
   当前不发布 Server 镜像。只有集中托管凭据或共享服务需求明确后，才重新评估 Server。
4. **RL 外部设施集成**：暂缓。训练侧管理 episode、隔离、执行超时、reward、文件和归档；
   E2B 等设施到该阶段再核实，不提前实现自有环境管理或适配框架。

## 旧实现迁移

新旧项目使用相同 import 名称，应使用独立虚拟环境。保留旧实现及来源、许可证，
不整体搬迁 broker、sandbox、session 或文档登记模型。
最终通过原公开仓库的迁移 PR 引入新实现，保留历史、tags、issues 和外部链接。
切换前固定旧版本和 legacy 分支，提供迁移说明；不强推覆盖或自动归档旧仓库。
旧 RL 用户继续通过固定版本复现，新基础包发布不代表 RL 已完成迁移。
