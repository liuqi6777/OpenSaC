# OpenSAC

**面向研究型智能体的开放、可审计 Search as Code 实现。**

[English](README.md) | [简体中文](README.zh-CN.md)

OpenSAC 将搜索从固定工具调用变成可编程接口。外部智能体生成 Python 程序，自由组合检索、正文阅读、
过滤、排序、结构化抽取与引用；OpenSAC 在隔离的 Docker 沙箱中执行程序，并由宿主机上的能力代理
（capability broker）统一处理所有特权操作。

本项目围绕一个核心研究问题设计：

> 在控制模型、检索后端和评测协议保持一致的条件下，让模型用生成的代码组合搜索原语，相比通过模型可见的
> 多轮工具调用组合相同原语，能否提升任务质量、上下文效率，或改善延迟—成本权衡？

OpenSAC 实现了公开的 [Search as Code](https://research.perplexity.ai/articles/rethinking-search-as-code-generation)
抽象，但并不试图复刻 Perplexity 的内部搜索引擎。

> [!IMPORTANT]
> OpenSAC 目前是持续开发中的研究原型（软件版本 `0.4.0`），API、文档和研究材料仍可能继续演进。

## 核心特性

- **可编程搜索流水线**：智能体可以用普通 Python 批量发起查询、融合排名、过滤和关联记录、检查覆盖率并选择证据。
- **紧凑的类型化 SDK**：通过 `opensac_sdk` 提供搜索、正文、状态、可选的结构化 LLM 抽取、用量和引用原语。
- **强化隔离执行**：生成的程序无法访问网络、服务商密钥、Docker socket 或不受限的宿主机文件系统。
- **上下文解耦**：大规模中间结果保留在程序工作空间中，只有程序明确打印或提交的数据才会返回控制模型。
- **贯穿全流程的来源追踪**：会话级不透明引用和 broker 签发的段落 locator 将候选结果连接到最终引用。
- **研究级观测能力**：会话预算、类型化局部失败、能力 trace、阶段耗时、幂等执行和 worker 生命周期控制支持可复现 rollout。
- **后端无关部署**：无需修改生成程序，即可在内置本地稠密检索器与 Serper + Jina Reader 网页检索之间切换。

## 系统设计

```text
外部智能体 / rollout harness
              |
              | 通过 POST /exec 提交生成的 Python
              v
       OpenSAC 会话 API  ---- 持久化 workspace 与 refs
              |
              v
       隔离的 Docker 沙箱
              |
              | opensac_sdk + 带认证的 Unix-socket RPC
              v
        宿主机 capability broker
          /           |              \
     本地检索      网页检索      可选 pipeline LLM
```

OpenSAC 有意不负责 agent loop。外部控制平面选择模型、生成程序、管理 rollout 并评测答案。每个 rollout
应复用同一个 OpenSAC session，使工作空间文件和不透明文档引用可以跨轮次使用。后端选择、密钥、重试、
限流和资源约束全部保留在服务端。

完整研究动机见[设计目标与能力路线图](docs/design.md)，当前能力契约见
[OpenSAC 0.4](docs/opensac-0.4.md)。

## 仓库结构

| 路径 | 用途 |
| --- | --- |
| `src/opensac/` | HTTP API、Python 客户端、capability broker、后端、沙箱和指标 |
| `packages/opensac-sdk/` | 嵌入生成程序中的类型化 SDK |
| `sandbox/` | 强化的 Docker 镜像与沙箱入口 |
| `sac_agent/` | 仅暴露一个 `sac_run(code)` 工具的最小 ReAct 控制智能体 |
| `local_search/` | 独立的 FAISS 稠密检索服务 |
| `skills/search-as-code/` | 供 coding agent 使用的 Search-as-Code skill |
| `skills/search-as-code-cli/` | 供纯 CLI 适配器使用的 Search-as-Code skill |
| `examples/` | SDK 示例程序与本地运行器 |
| `tests/` | 单元、集成、安全与 Docker 端到端测试 |
| `docs/` | 设计、部署、研究观测与版本文档 |
| `paper/opensac/` | 正在撰写的论文源文件 |

## 环境要求

- Python 3.12 或更高版本
- [`uv`](https://docs.astral.sh/uv/)
- Docker，用于隔离执行
- 至少一个搜索后端：
  - 内置本地检索器、对应 FAISS 索引，以及足够的内存/GPU 资源；或
  - 用于网页检索的 Serper 和 Jina API 凭证
- 可选：用于 `sdk.llm.*` 的 OpenAI 兼容 Chat Completions 服务

## 快速开始

### 1. 安装

```bash
git clone https://github.com/liuqi6777/OpenSaC.git
cd OpenSaC
uv sync --extra dev
cp .env.example .env
```

部署前请检查 `.env`。空的 `OPENSAC_API_KEY` 仅适用于可信的本地开发环境；只要 API 会暴露到
localhost 之外，就应设置强 bearer token。

### 2. 配置一个搜索后端

#### 方案 A：本地稠密检索

内置服务加载预先生成的 BrowseComp-Plus FAISS 索引，不会训练或重建索引。

```bash
./local_search/run setup
./local_search/run prepare --revision COMMIT_SHA  # 为可复现性固定版本
./local_search/run
```

最后一条命令会在 `127.0.0.1:8081` 前台运行。在 `.env` 中保留：

```bash
OPENSAC_SEARCH_BACKEND=local
OPENSAC_LOCAL_SEARCH_BASE_URL=http://127.0.0.1:8081
```

首次启动还会下载 `Qwen/Qwen3-Embedding-8B`。CPU 模式可用，但需要较大内存且速度会慢很多。
精确的数据格式、设备选择和健康检查参见[本地稠密检索](docs/local-search.md)。

#### 方案 B：网页检索

```bash
export OPENSAC_SEARCH_BACKEND=web
export OPENSAC_SERPER_API_KEY=your-serper-key
export OPENSAC_JINA_API_KEY=your-jina-key
```

网页后端使用 Serper 检索结果，并通过 Jina Reader 获取正文。请将这些凭证保留在 OpenSAC 宿主机上，
不要写入生成程序。

### 3. 构建并启动 OpenSAC

```bash
uv run opensac build-sandbox
uv run opensac serve
```

升级仓库后应重新构建沙箱镜像。在另一个终端中检查服务：

```bash
curl -fsS http://127.0.0.1:8000/healthz
```

### 4. 执行 Search-as-Code 程序

下面的示例创建一个 session，运行一段生成式 Python，打印结构化结果，并确保最后删除 session：

```bash
uv run python - <<'PY'
import os

from opensac import OpenSAC

program = '''
from opensac_sdk import sdk

batches = sdk.search.many(
    ["ReAct paper", "ReAct reasoning acting language models"],
    limit_per_query=5,
    concurrency=2,
)
fusion = sdk.search.fuse_rrf(batches, k=60, limit=5)
refs = [candidate.ref for candidate in fusion.candidates]
passages = sdk.content.snippets("Who introduced ReAct?", refs, max_tokens=2000)

sdk.output.submit(
    {"passages": [passage.model_dump() for passage in passages]},
    citations=[
        {"ref": passage.ref, "locator": passage.locator}
        for passage in passages
        if passage.locator is not None
    ],
)
'''

with OpenSAC(api_key=os.getenv("OPENSAC_API_KEY", "")) as client:
    session = client.create_session()
    try:
        result = client.exec_code(session["id"], program, include_trace=True)
        print(result["output"])
        print(result["usage"])
    finally:
        client.delete_session(session["id"])
PY
```

包含多查询融合、来源过滤、JSONL 持久化状态和段落引用的完整示例见
[`examples/research_pipeline.py`](examples/research_pipeline.py)。如果希望在没有控制模型的情况下迭代
SDK 程序，可以运行：

```bash
uv run python examples/run_sdk_locally.py examples/research_pipeline.py
# 添加 --docker 可测试真实沙箱和代码校验器。
```

宿主机模式只用于开发，不会应用容器隔离或沙箱校验。

## SDK 接口

生成程序使用 `from opensac_sdk import sdk` 导入单例。

| 命名空间 | 主要操作 | 作用 |
| --- | --- | --- |
| `sdk.search` | `search(...)`、`many(...)`、`fuse_rrf(...)` | 检索和融合候选，同时保留 provenance |
| `sdk.content` | `get_many(...)`、`snippets(...)`、`grep(...)`、`grep_report(...)`、`read(...)` | 获取、定位和检查证据 |
| `sdk.llm` | `map(...)`、`map_many(...)`、`extract(...)`、`extract_many(...)` | 可选的 broker 模型调用和 schema 校验抽取 |
| `sdk.state` | JSON/JSONL 与 workspace 辅助方法 | 在同一 session 的多次执行间持久化显式状态 |
| `sdk.session` | `usage()` | 查看紧凑的策略统计和剩余预算 |
| `sdk.output` | `submit(...)` | 返回结构化输出并解析可信引用 |

批量操作保持输入对齐，并为每一行暴露类型化失败。空搜索结果属于成功结果，不是失败。段落级引用必须使用
正文操作返回的 locator，客户端不得自行构造。完整行为和迁移说明见
[OpenSAC 0.4](docs/opensac-0.4.md)。

## 智能体集成

OpenSAC 支持三种驱动方式：

1. **自定义 agent loop**：将 `/v1/sessions/{session_id}/exec` 包装为模型可见的单一
   `sac_run(code)` 工具，并在一个 rollout 内复用 session。可运行的
   [`sac_agent`](sac_agent/README.md) 展示了最小 OpenAI 兼容 ReAct 循环。
2. **通过纯 CLI 接入 coding agent**：安装
   [`skills/search-as-code-cli`](skills/search-as-code-cli/SKILL.md)，将每段生成的 Python 程序通过
   stdin 交给 `opensac agent-run`。适配器从宿主环境派生对话上下文，模型不接触 session ID。
3. **通过 MCP 接入 coding agent**：以本地 stdio 服务运行 `opensac mcp`，并安装
   [`skills/search-as-code`](skills/search-as-code/SKILL.md)。公开执行接口只有
   `sac_run(code)`；对话身份、session 创建、lease 续租和恢复都由 MCP 适配层负责，不进入模型参数。

`sac_agent` 使用的控制模型端点，与沙箱内通过 `sdk.llm.*` 暴露的可选 pipeline 模型端点彼此独立。

### 纯 CLI（无需 MCP）

安装命令与 CLI 专用 skill，然后启动 OpenSAC API：

```bash
export OPENSAC_REPO=/absolute/path/to/OpenSaC
uv tool install --editable "$OPENSAC_REPO"

# Codex
mkdir -p ~/.codex/skills
cp -R "$OPENSAC_REPO/skills/search-as-code-cli" ~/.codex/skills/

# Claude Code
mkdir -p ~/.claude/skills
cp -R "$OPENSAC_REPO/skills/search-as-code-cli" ~/.claude/skills/

export SAC_API_BASE=http://127.0.0.1:8000
export SAC_API_KEY=replace-with-your-opensac-key
```

agent 通过 stdin 执行一段程序：

```bash
opensac agent-run <<'OPENSAC_PY'
from opensac_sdk import sdk
print(sdk.search("OpenSAC Search as Code", limit=3))
OPENSAC_PY
```

本地 Codex task 通过隔离的 `CODEX_THREAD_ID` 兼容适配器解析；Claude Code shell 使用
`CLAUDE_CODE_SESSION_ID`。两者都不存在时，命令会 fail closed，不会退化为按进程或工作目录共享。
其他 CLI agent 必须在子进程环境中同时设置 `SAC_AGENT_CONTEXT_ID` 和稳定的小写
`SAC_AGENT_HOST`。

| 环境变量 | 默认值 | 用途 |
| --- | --- | --- |
| `SAC_API_BASE` | `http://127.0.0.1:8000` | 适配层访问的 OpenSAC API |
| `SAC_API_KEY` | 空，随后回退到 `OPENSAC_API_KEY` | Bearer 凭据；不会写入 registry |
| `SAC_CLI_LEASE_SECONDS` | `3600` | 可续租 session lease，范围为 `1` 到 `86400` 秒 |
| `SAC_CLI_STATE_DIR` | 平台用户状态目录 | CLI SQLite generation registry 的位置 |
| `SAC_AGENT_CONTEXT_ID` | 未设置 | 其他 CLI agent 显式提供的对话 ID |
| `SAC_AGENT_HOST` | `cli` | 与显式对话 ID 配对的 namespace |

原始对话 ID 会先结合 host namespace 做 SHA-256 派生，再进入持久化状态。每次 CLI 调用退出时只关闭
HTTP client，带 lease 的服务端 session 仍可恢复。服务端返回 `session_expired` 或
`worker_restarted` 时不会自动重放失败程序；本次返回 `state_lost`，下一次调用进入干净的新
generation。Claude Code 官方文档说明了其子进程 session 变量和个人 skill 目录，见
[环境变量](https://code.claude.com/docs/en/env-vars)与
[skills](https://code.claude.com/docs/en/skills)。

### Codex MCP

先启动 OpenSAC API，再使用仓库绝对路径注册 MCP 服务：

```bash
export OPENSAC_REPO=/absolute/path/to/OpenSaC
export SAC_API_BASE=http://127.0.0.1:8000
export SAC_API_KEY=replace-with-your-opensac-key

codex mcp add \
  --env SAC_API_BASE="$SAC_API_BASE" \
  --env SAC_API_KEY="$SAC_API_KEY" \
  opensac -- uv --directory "$OPENSAC_REPO" run opensac mcp
```

Codex 会在 MCP 请求 metadata 中提供当前 task 身份。如果该字段缺失，适配层会 fail closed，不会退化为
按工作目录或整个进程共享 session。

### Claude Code MCP

注册同一个 stdio 服务：

```bash
export OPENSAC_REPO=/absolute/path/to/OpenSaC
export SAC_API_BASE=http://127.0.0.1:8000
export SAC_API_KEY=replace-with-your-opensac-key

claude mcp add --scope user opensac \
  -e SAC_API_BASE="$SAC_API_BASE" \
  -e SAC_API_KEY="$SAC_API_KEY" \
  -- uv --directory "$OPENSAC_REPO" run opensac mcp
```

将下面的 hook 合并到 `~/.claude/settings.json`。每次调用 `sac_run` 前，它会把 Claude Code 官方 hook
输入中的 `session_id` 传给宿主专用的 `bind_context` 工具，agent 不参与绑定：

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "mcp__opensac__sac_run",
        "hooks": [
          {
            "type": "mcp_tool",
            "server": "opensac",
            "tool": "bind_context",
            "input": { "context_id": "${session_id}" }
          }
        ]
      }
    ]
  }
}
```

如果 hook 没有完成绑定，`sac_run` 会 fail closed。Search-as-Code skill 也明确将 `bind_context`
保留给宿主 hook，禁止模型主动调用。

### MCP 配置与生命周期

| 环境变量 | 默认值 | 用途 |
| --- | --- | --- |
| `SAC_API_BASE` | `http://127.0.0.1:8000` | 适配层访问的 OpenSAC API |
| `SAC_API_KEY` | 空，随后回退到 `OPENSAC_API_KEY` | Bearer 凭据；不会写入 MCP registry |
| `SAC_MCP_LEASE_SECONDS` | `3600` | 可续租 session lease，范围为 `1` 到 `86400` 秒 |
| `SAC_MCP_STATE_DIR` | 平台用户状态目录 | SQLite generation registry 的位置 |

原始 Codex/Claude 对话 ID 在进入 request ID 或 SQLite 前，会先结合 host namespace 做 SHA-256
派生。一个 task 的多次调用复用一个带 lease 的 OpenSAC session，MCP 重启后仍可恢复。MCP 退出只关闭
HTTP client，不删除 session。如果服务端返回 `session_expired` 或 `worker_restarted`，失败的程序不会
自动重放；本次返回 `state_lost`，下一次调用使用干净的新 generation。宿主配置细节可参考官方
[Codex MCP](https://learn.chatgpt.com/docs/extend/mcp?surface=cli) 与
[Claude Code hooks](https://code.claude.com/docs/en/hooks) 文档。

## TODO

- 后续发布 benchmark 协议、可复现实验配置、运行 trace 与结果。
- 论文公开后补充稳定的论文引用信息。

## 文档

- [设计目标与能力路线图](docs/design.md)
- [OpenSAC 0.4 版本与迁移说明](docs/opensac-0.4.md)
- [本地稠密检索](docs/local-search.md)
- [研究观测](docs/research-instrumentation.md)
- [RL worker 部署](docs/rl-environment-workers.md)
- [工具能力差距](docs/tool-capability-gaps.md)
- [高 fan-out 可靠性计划](docs/opensac-0.3-plan.md)

## 局限性

- OpenSAC 是研究运行时，不是托管搜索产品，也不是完整的智能体框架。
- 真正的隔离依赖 Docker；宿主机模式 SDK runner 不构成安全边界。
- 内置本地检索器使用大型 embedding 模型和预先生成的索引，资源受限的机器可能难以运行。
- 网页检索的质量、可用性、延迟与成本依赖外部服务商。
- 沙箱只能降低风险；多租户部署仍需宿主机加固、网络控制、认证、监控与文件系统配额。

## 参与贡献

欢迎提交 issue 和范围清晰的 pull request。请保持改动小而易审查，记录行为变化，并在适用时增加或更新测试。
提交 pull request 前请运行：

```bash
uv run ruff check .
uv run pytest
```

如果修改公开 SDK 行为，还应同步更新 `docs/` 下的能力契约或版本说明。

## 引用

论文送审期间，如果 OpenSAC 对你的研究有帮助，请引用本仓库：

```bibtex
@software{liu2026opensac,
  author  = {Qi Liu},
  title   = {OpenSAC: An Open Implementation of Search as Code},
  year    = {2026},
  url     = {https://github.com/liuqi6777/OpenSaC},
  version = {0.4.0}
}
```

讨论该架构时，也请引用最初的 Search-as-Code 工作。

## 许可证

OpenSAC 使用 [MIT License](LICENSE) 发布。
