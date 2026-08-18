# 智能体集成

[English](agent-integrations.md) | [简体中文](agent-integrations.zh-CN.md)

OpenSAC 提供执行运行时，而不负责控制循环。智能体集成应只向模型暴露一个操作 `sac_run(code)`，并让
同一个 rollout 或对话复用同一个 OpenSAC session。

## 选择集成方式

| 集成方式 | 适用场景 | 对话/session 管理方 |
| --- | --- | --- |
| 自定义 HTTP/Python loop | 已有自己的 agent harness | 你的应用 |
| `opensac agent-run` | Coding agent 可以执行 shell 命令 | CLI 适配层 |
| `opensac mcp` | Codex 或 Claude Code 通过一个 MCP 工具调用 | MCP 适配层 |

外部智能体使用的控制模型端点，与沙箱程序通过 `sdk.llm.*` 使用的可选 pipeline 模型端点彼此独立。

## 前置条件

首次正式发布前，先从源码检出启动 OpenSAC API；公开 Compose 镜像当前还不可用，项目也不计划发布
PyPI 包。宿主机侧适配命令从同一份源码安装：

```bash
uv tool install --editable /absolute/path/to/OpenSaC

export SAC_API_BASE=http://127.0.0.1:8000
export SAC_API_KEY=replace-with-your-opensac-key
```

Skill 随仓库进行版本控制，不嵌入 Python wheel。当前让 `OPENSAC_REPO` 指向正在使用的源码检出；
Docker 正式版本发布后，应检出与运行中服务相同的标签：

```bash
export OPENSAC_REPO=/absolute/path/to/OpenSaC
```

不要在可提交的项目配置中写入明文 API key；应引用环境变量或使用用户本地配置。

## 自定义 agent loop

将 `POST /v1/sessions/{session_id}/exec` 或 `OpenSAC.exec_code` 包装成单一的 `sac_run(code)` 工具。
Rollout 开始时创建 session，跨轮次复用，并在结束时删除或中止。这样既能保留工作空间文件与不透明文档
引用，又不会让模型生成或处理 session ID。

可运行的 [sac_agent](../sac_agent/README.md) 展示了最小 OpenAI 兼容 ReAct loop。正式 harness 还应
处理 lease、`worker_restarted`、`session_expired`、请求幂等性与 worker affinity。

## 选择项目级或全局级作用域

适配命令每个用户只需安装一次；skill 和 MCP 注册可以限制在一个项目，也可以对该用户的所有项目生效：

| 宿主 | 项目级 | 全局/用户级 |
| --- | --- | --- |
| Codex skill | `<project>/.agents/skills/` | `~/.agents/skills/` |
| Codex MCP | `<project>/.codex/config.toml` | Codex 用户配置 |
| Claude Code skill | `<project>/.claude/skills/` | `~/.claude/skills/` |
| Claude Code MCP | `<project>/.mcp.json` | 使用 `--scope user` 的用户配置 |

团队仓库或只希望在单个项目中使用 OpenSAC 时选择项目级；个人需要跨仓库复用时选择全局级。

本仓库以 `.agents/skills/` 作为 skill 的唯一源码，`.claude/skills` 指向同一目录，因此无需维护两份副本。

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
| `SAC_CLI_LEASE_SECONDS` | `3600` | 可续租 session lease，范围为 `1` 到 `86400` 秒 |
| `SAC_CLI_STATE_DIR` | 平台用户状态目录 | CLI SQLite generation registry |
| `SAC_AGENT_CONTEXT_ID` | 未设置 | 其他 CLI agent 显式提供的对话 ID |
| `SAC_AGENT_HOST` | `cli` | 与显式对话 ID 配对的 namespace |

适配层会先结合 host namespace 对原始对话 ID 做 SHA-256 派生，再持久化。单次调用退出时只关闭 HTTP
client，带 lease 的服务端 session 仍可恢复。如果服务端返回 `session_expired` 或
`worker_restarted`，适配层不会重放失败程序；本次返回 `state_lost`，下一次调用从干净 generation 开始。

## MCP 集成

MCP 方式使用 `search-as-code` skill。公开工具只有 `sac_run(code)`；对话绑定与生命周期工具仅供宿主使用。

### 安装 MCP skill

```bash
export AGENT_PROJECT=/absolute/path/to/your/project

# 项目级——按实际使用的宿主执行对应复制命令。
mkdir -p "$AGENT_PROJECT/.agents/skills" "$AGENT_PROJECT/.claude/skills"
cp -R "$OPENSAC_REPO/.agents/skills/search-as-code" "$AGENT_PROJECT/.agents/skills/"
cp -R "$OPENSAC_REPO/.agents/skills/search-as-code" "$AGENT_PROJECT/.claude/skills/"

# 全局级——按实际使用的宿主执行对应复制命令。
mkdir -p ~/.agents/skills ~/.claude/skills
cp -R "$OPENSAC_REPO/.agents/skills/search-as-code" ~/.agents/skills/
cp -R "$OPENSAC_REPO/.agents/skills/search-as-code" ~/.claude/skills/
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

### Claude Code MCP

项目级接入将以下内容加入 `<project>/.mcp.json`。API key 从每位用户的环境展开，不以明文提交：

```json
{
  "mcpServers": {
    "opensac": {
      "type": "stdio",
      "command": "opensac",
      "args": ["mcp"],
      "env": {
        "SAC_API_BASE": "${SAC_API_BASE:-http://127.0.0.1:8000}",
        "SAC_API_KEY": "${SAC_API_KEY}"
      }
    }
  }
}
```

Claude Code 首次使用项目级 MCP server 时会请求确认。全局/用户级接入：

```bash
claude mcp add \
  --scope user \
  --env SAC_API_BASE="$SAC_API_BASE" \
  --env SAC_API_KEY="$SAC_API_KEY" \
  --transport stdio \
  opensac \
  -- opensac mcp
```

Claude Code 还支持 `--scope local`，用于保存在仓库外的项目级个人配置。

最后将对话绑定 hook 合并到 `<project>/.claude/settings.json` 或 `~/.claude/settings.json`：

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

如果 hook 没有完成对话绑定，`sac_run` 会 fail closed。Search-as-Code skill 将 `bind_context` 保留给
宿主 hook，模型不得主动调用。

### MCP 配置与生命周期

| 环境变量 | 默认值 | 用途 |
| --- | --- | --- |
| `SAC_API_BASE` | `http://127.0.0.1:8000` | 适配层访问的 OpenSAC API |
| `SAC_API_KEY` | 空，随后回退到 `OPENSAC_API_KEY` | Bearer 凭据；不会写入 MCP registry |
| `SAC_MCP_LEASE_SECONDS` | `3600` | 可续租 session lease，范围为 `1` 到 `86400` 秒 |
| `SAC_MCP_STATE_DIR` | 平台用户状态目录 | MCP SQLite generation registry |

原始 Codex/Claude 对话 ID 在进入 request ID 或 SQLite 前，会先结合 host namespace 做 SHA-256 派生。
一个 task 复用一个带 lease 的 OpenSAC session，并能在 MCP 重启后恢复。MCP 退出只关闭 HTTP client，
不会删除 session。遇到 `session_expired` 或 `worker_restarted` 时不会重放失败程序；本次调用返回
`state_lost`，下一次调用从干净 generation 开始。

## 安全与正确性规则

- 只向模型暴露 `sac_run(code)`；对话绑定与凭证必须保留在宿主侧。
- 一个 session 只能复用于一个可信的对话或 rollout 身份。
- 不要在项目文件或适配层 registry 中持久化原始对话 ID 与 API key。
- 状态丢失后不要自动重放执行；上一次执行结果可能处于不确定状态。
- 适配层与 OpenSAC 服务应使用兼容版本。

宿主配置行为可参考官方 [Codex MCP](https://developers.openai.com/codex/mcp)、
[Codex skills](https://developers.openai.com/codex/skills)、
[Claude Code MCP](https://code.claude.com/docs/en/mcp) 与
[Claude Code hooks](https://code.claude.com/docs/en/hooks) 文档。
