# 智能体集成

[English](agent-integrations.md) | [简体中文](agent-integrations.zh-CN.md)

OpenSAC 提供执行运行时，而不负责控制循环。智能体集成应只向模型暴露一个操作 `sac_run(code)`，并让
同一个 rollout 或对话复用同一个 OpenSAC session。

## 选择集成方式

| 集成方式 | 适用场景 | 对话/session 管理方 |
| --- | --- | --- |
| 自定义 HTTP/Python loop | 已有自己的 agent harness | 你的应用 |
| `opensac agent-run` | Coding agent 可以执行 shell 命令 | CLI 适配层 |
| `opensac mcp` | Codex 通过一个 MCP 工具调用 | MCP 适配层 |

外部智能体使用的控制模型端点，与沙箱程序通过 `sdk.llm.*` 使用的可选 pipeline 模型端点彼此独立。

## 前置条件

按照主 README 的 [Docker 快速开始](../README.zh-CN.md#使用-docker-快速开始)启动公开的 `v0.8.3`
服务镜像，服务本身不需要源码检出。项目不发布 PyPI 包，因此使用 CLI 或 MCP 适配器的宿主机需要检出
相同的发布版本，以安装适配命令和 skill：

```bash
git clone https://github.com/liuqi6777/OpenSaC.git
cd OpenSaC
git checkout v0.8.3
uv tool install --editable '/absolute/path/to/OpenSaC[mcp]'

export SAC_API_BASE=http://127.0.0.1:8000
export SAC_API_KEY=replace-with-your-opensac-key
```

Skill 随仓库进行版本控制，不嵌入 Python wheel。让 `OPENSAC_REPO` 指向与运行中服务版本一致的源码
检出：

```bash
export OPENSAC_REPO=/absolute/path/to/OpenSaC
```

不要在可提交的项目配置中写入明文 API key；应引用环境变量或使用用户本地配置。

基础包足以运行 `opensac agent-run`。MCP 适配器需要 `mcp` extra，捆绑的 control loop 需要
`agent`，配置了 `OPENSAC_MODEL_NAME` 的源码服务需要 `llm`；`full` 会安装这三组能力。

## 自定义 agent loop

将 `POST /v1/sessions/{session_id}/exec` 或 `OpenSAC.exec_code` 包装成单一的 `sac_run(code)` 工具。
Rollout 开始时创建 session，跨轮次复用，并在结束时删除或中止。默认的
`execution_mode="program"` 既能保留工作空间文件与受 session 约束的本地文档 ID，又不会让模型生成或
处理 session ID；公开来源 URL 可以跨 session 传递。实验模式则使用
`OpenSAC.create_session(execution_mode="persistent_interpreter")`，详见下文。

应在 stdout 之前展示执行响应中有界的 `warnings` 列表。即使生成代码只打印成功值，这些 warning
也能暴露局部或全部外部 item 失败，同时不会让原本成功的执行变成失败。

可运行的 [sac_agent](../sac_agent/README.md) 展示了最小 OpenAI 兼容 ReAct loop。正式 harness 还应
处理 lease、`worker_restarted`、`session_expired`、请求幂等性与 worker affinity。

## 选择项目级或全局级作用域

适配命令每个用户只需安装一次；skill 和 MCP 注册可以限制在一个项目，也可以对该用户的所有项目生效：

| 宿主 | 项目级 | 全局/用户级 |
| --- | --- | --- |
| Codex skill | `<project>/.agents/skills/` | `~/.agents/skills/` |
| Codex MCP | `<project>/.codex/config.toml` | Codex 用户配置 |
| Claude Code skill | `<project>/.claude/skills/` | `~/.claude/skills/` |

团队仓库或只希望在单个项目中使用 OpenSAC 时选择项目级；个人需要跨仓库复用时选择全局级。

本仓库以 `.agents/skills/` 作为 skill 的唯一源码，`.claude/skills` 指向同一目录，因此 CLI skill 无需维护两份副本。

## 纯 CLI 集成

CLI 方式使用 `search-as-code-cli` skill，并将每段程序通过 stdin 交给 `opensac agent-run`。

### 安装 CLI skill

`AGENT_PROJECT` 是 coding agent 准备使用 OpenSAC 的目标仓库：

```bash
export AGENT_PROJECT=/absolute/path/to/your/project

# 项目级——按实际使用的宿主执行对应复制命令。
mkdir -p "$AGENT_PROJECT/.agents/skills" "$AGENT_PROJECT/.claude/skills"
cp -R "$OPENSAC_REPO/.agents/skills/search-as-code-cli" "$AGENT_PROJECT/.agents/skills/"
cp -R "$OPENSAC_REPO/.agents/skills/search-as-code-cli" "$AGENT_PROJECT/.claude/skills/"

# 全局级——按实际使用的宿主执行对应复制命令。
mkdir -p ~/.agents/skills ~/.claude/skills
cp -R "$OPENSAC_REPO/.agents/skills/search-as-code-cli" ~/.agents/skills/
cp -R "$OPENSAC_REPO/.agents/skills/search-as-code-cli" ~/.claude/skills/
```

直接测试适配层：

```bash
opensac agent-run <<'OPENSAC_PY'
from opensac_sdk import sdk
print(sdk.search("OpenSAC Search as Code", limit=3))
OPENSAC_PY
```

本地 Codex task 使用 `CODEX_THREAD_ID`，Claude Code shell 使用 `CLAUDE_CODE_SESSION_ID`。两者都不存在
时，命令会 fail closed，不会退化为按进程或工作目录共享 session。其他 CLI agent 必须显式设置对话身份：

```bash
export SAC_AGENT_CONTEXT_ID=stable-conversation-id
export SAC_AGENT_HOST=my-agent
```

### CLI 配置与生命周期

| 环境变量 | 默认值 | 用途 |
| --- | --- | --- |
| `SAC_API_BASE` | `http://127.0.0.1:8000` | 适配层访问的 OpenSAC API |
| `SAC_API_KEY` | 空，随后回退到 `OPENSAC_API_KEY` | Bearer 凭据；不会写入 registry |
| `SAC_CLI_EXECUTION_MODE` | `program` | Session 执行模式；实验组使用 `persistent_interpreter` |
| `SAC_CLI_LEASE_SECONDS` | `3600` | 可续租 session lease，范围为 `1` 到 `86400` 秒 |
| `SAC_CLI_STATE_DIR` | 平台用户状态目录 | CLI SQLite generation registry |
| `SAC_AGENT_CONTEXT_ID` | 未设置 | 其他 CLI agent 显式提供的对话 ID |
| `SAC_AGENT_HOST` | `cli` | 与显式对话 ID 配对的 namespace |

适配层会先结合 host namespace 对原始对话 ID 做 SHA-256 派生，再持久化。单次调用退出时只关闭 HTTP
client，带 lease 的服务端 session 仍可恢复。如果服务端返回 `session_expired` 或
`worker_restarted`，适配层不会重放失败程序；本次返回 `state_lost`，下一次调用从干净 generation 开始。

## MCP 集成

Codex MCP 方式使用 `search-as-code` skill，并且只暴露 `sac_run(code)`。Claude Code 因为不会在 MCP
request metadata 中提供对话身份，使用上文的 CLI 方式。

### 安装 MCP skill

```bash
export AGENT_PROJECT=/absolute/path/to/your/project

# 项目级。
mkdir -p "$AGENT_PROJECT/.agents/skills"
cp -R "$OPENSAC_REPO/.agents/skills/search-as-code" "$AGENT_PROJECT/.agents/skills/"

# 全局级。
mkdir -p ~/.agents/skills
cp -R "$OPENSAC_REPO/.agents/skills/search-as-code" ~/.agents/skills/
```

### Codex MCP

项目级接入将以下内容合并到 `<project>/.codex/config.toml`。Codex 只会在项目被信任后读取项目配置。
启动 Codex 前导出 `SAC_API_KEY`：

```toml
[mcp_servers.opensac]
command = "opensac"
args = ["mcp"]
env_vars = ["SAC_API_KEY"]

[mcp_servers.opensac.env]
SAC_API_BASE = "http://127.0.0.1:8000"
```

全局/用户级接入：

```bash
codex mcp add opensac \
  --env SAC_API_BASE="$SAC_API_BASE" \
  --env SAC_API_KEY="$SAC_API_KEY" \
  -- opensac mcp
```

Codex 会在 MCP 请求 metadata 中提供当前 task 身份。该字段缺失时，适配层会 fail closed，不会退化为
按整个进程或工作目录共享 session。

### MCP 配置与生命周期

| 环境变量 | 默认值 | 用途 |
| --- | --- | --- |
| `SAC_API_BASE` | `http://127.0.0.1:8000` | 适配层访问的 OpenSAC API |
| `SAC_API_KEY` | 空，随后回退到 `OPENSAC_API_KEY` | Bearer 凭据；不会写入 MCP registry |
| `SAC_MCP_EXECUTION_MODE` | `program` | Session 执行模式；实验组使用 `persistent_interpreter` |
| `SAC_MCP_LEASE_SECONDS` | `3600` | 可续租 session lease，范围为 `1` 到 `86400` 秒 |
| `SAC_MCP_STATE_DIR` | 平台用户状态目录 | MCP SQLite generation registry |

原始 Codex task ID 在进入 request ID 或 SQLite 前，会先结合 host namespace 做 SHA-256 派生。
一个 task 复用一个带 lease 的 OpenSAC session，并能在 MCP 重启后恢复。MCP 退出只关闭 HTTP client，
不会删除 session。遇到 `session_expired` 或 `worker_restarted` 时不会重放失败程序；本次调用返回
`state_lost`，下一次调用从干净 generation 开始。

## 实验性持久解释器

服务端和适配层都必须显式启用该特性。仅在隔离的实验中设置：

```bash
# OpenSAC 服务配置
export OPENSAC_EXPERIMENTAL_PERSISTENT_INTERPRETER=true

# 实验组进程只选择一种适配方式。
export SAC_CLI_EXECUTION_MODE=persistent_interpreter
# 或：export SAC_MCP_EXECUTION_MODE=persistent_interpreter
```

每个实验组 session 在第一次执行时懒启动一个内部 `default` Python 解释器。顶层变量、函数、import，
以及普通异常发生前已经完成的赋值会保留到下一次 `sac_run`。`mechanisms.persistence` 仍然只控制文件：
设为 false 时，每个 cell 使用临时 workspace，但 Python globals 继续存在。持久 session 会固定占用其
sandbox 容器直到删除或过期，因此容量规划应按并发实验 session 计算，而不是依赖 warm LRU 上限。

将实验 skill 与 baseline skill 并列安装：

```bash
# MCP 实验 skill（Codex）
cp -R "$OPENSAC_REPO/.agents/skills/search-as-code-repl" "$AGENT_PROJECT/.agents/skills/"

# CLI 实验 skill（Codex 或 Claude Code）
cp -R "$OPENSAC_REPO/.agents/skills/search-as-code-repl-cli" "$AGENT_PROJECT/.agents/skills/"
cp -R "$OPENSAC_REPO/.agents/skills/search-as-code-repl-cli" "$AGENT_PROJECT/.claude/skills/"
```

REPL skill 禁止隐式调用。实验组 prompt 必须明确写 `$search-as-code-repl` 或
`$search-as-code-repl-cli`；baseline 继续使用现有 skill 和 `program` 模式。第一条 observation 会报告
实际 execution mode，REPL skill 在模式不匹配时立即报告配置错误。

响应会报告 `interpreter_state`（`not_started`、`ready` 或 `lost`）、可选的 loss reason，以及顶层用户
symbol 数量；不会记录 symbol 名称或值。超时、输出超限、kernel 退出、协议损坏或遗留后台线程都会将
解释器标为 lost 并删除容器。失败 cell 不会被重放，直接继续执行该 session 会得到
`410 interpreter_lost`，适配层则会在下一次调用时切换到干净 session。

## 安全与正确性规则

- 只向模型暴露 `sac_run(code)`；对话绑定与凭证必须保留在宿主侧。
- 一个 session 只能复用于一个可信的对话或 rollout 身份。
- 不要在项目文件或适配层 registry 中持久化原始对话 ID 与 API key。
- 状态丢失后不要自动重放执行；上一次执行结果可能处于不确定状态。
- 适配层与 OpenSAC 服务应使用兼容版本。

宿主配置行为可参考官方 [Codex MCP](https://developers.openai.com/codex/mcp) 和
[Codex skills](https://developers.openai.com/codex/skills) 文档。
