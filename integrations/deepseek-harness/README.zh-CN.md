# opensac-dsh

[English](README.md) | [简体中文](README.zh-CN.md)

`opensac-dsh` 是可安装的 DeepSeek Harness bundle，只向模型暴露一个原生 OpenSAC 工具：

```text
sac_run({ code: string }) -> string
```

插件负责提供能力；随包注册的 `search-as-code-dsh` skill 负责教模型编写有证据约束的研究程序。Skill
本身不调用 shell，也不管理 session。

## 为什么不是纯 skill

纯 skill 只能提示模型使用通用 shell 工具。插件可以把当前 `exec.agent.id` 稳定绑定到 OpenSAC
session，把取消信号传给子进程，通过 dsh 解析凭证，固定执行无 shell 的命令，并让有状态调用保持串行。
这些属于运行时保证，而不是提示词约定。

## 前置条件

- 与 DeepSeek Harness `0.1.0-rc.7` 兼容的包、Node.js `^22.19.0` 或 `>=24.0.0`，以及
  pnpm `11.7.0`。
- 正在运行的 OpenSAC `0.4.x` 服务。
- dsh 所在宿主机已安装同版本的 `opensac` CLI。

OpenSAC 不发布 PyPI 包，因此需要从版本匹配的源码检出安装 CLI：

```bash
export OPENSAC_REPO=/absolute/path/to/OpenSaC
uv tool install --editable "$OPENSAC_REPO"
```

## 构建与安装

把本地 checkout 加入 dsh profile 前，先构建 TypeScript 包：

```bash
cd "$OPENSAC_REPO/integrations/deepseek-harness"
corepack pnpm install --frozen-lockfile
corepack pnpm build

dsh plugin --profile web add "$OPENSAC_REPO/integrations/deepseek-harness"
```

可将 `web` 换成需要启用工具的 profile。若要交付可复制的产物，在构建后运行
`corepack pnpm pack`，再把生成的 `.tgz` 加入 profile。

启动 dsh 前导出服务配置。只有 OpenSAC 服务明确未启用鉴权时才应省略 `SAC_API_KEY`：

```bash
export SAC_API_BASE=http://127.0.0.1:8000
export SAC_API_KEY=replace-with-your-opensac-key
dsh --profile web
```

Bundle 会贡献 id 为 `opensac` 的 Cordis row，后续 profile patch 可以覆盖它。dsh 对同一 row 的
`config` 采用整体替换而不是深度合并，因此覆盖时需要写全所有非默认值。

## 配置

| 字段 | 默认值 | 用途 |
| --- | --- | --- |
| `command` | `opensac` | 插件加载时解析的绝对可执行文件或 PATH 命令名 |
| `apiBase` | `http://127.0.0.1:8000` | OpenSAC HTTP(S) 服务地址 |
| `apiKeyEnv` | `SAC_API_KEY` | 每次工具调用重新解析的 dsh credential 引用 |
| `leaseSeconds` | `3600` | CLI session 可续租时长，范围 1 到 86400 秒 |
| `stateDir` | OpenSAC 平台默认值 | 可选的 CLI SQLite generation registry 目录 |
| `cwd` | dsh 启动目录 | 子进程工作目录 |
| `timeoutMs` | `310000` | dsh 协作式工具超时 |
| `maxOutputBytes` | `262144` | stdout/stderr 各自的保留上限 |
| `graceMs` | `1000` | 进程树从 SIGTERM 到 SIGKILL 的宽限时间 |

`SAC_API_BASE`、`SAC_CLI_LEASE_SECONDS` 和 `SAC_CLI_STATE_DIR` 会通过
`cordis.patch.yml` 初始化 bundle 配置。`apiKeyEnv` 只是凭证名称，patch 不保存明文 key。

## 生命周期与安全边界

- 模型公开面只有 `sac_run(code)`；上下文 id 和凭证由宿主持有。
- CLI 使用固定 argv（`opensac agent-run`），程序从 stdin 输入，不让 shell 解析模型内容。
- dsh 会清洗子进程的环境变量；插件只显式传递 OpenSAC 配置和本次调用解析到的凭证。
- `sac_run` 不声明并发安全，因此 dsh 会把调用作为 exclusive barrier 串行执行。
- 输出有明确上限；发生截断时直接失败，不返回不完整证据。
- 现有 OpenSAC CLI registry 会先哈希原始 dsh agent id 再持久化，并跨调用、跨 dsh 重启续租
  service session。
- `state_lost` 作为 observation 返回，绝不自动重放。卸载插件也不会删除仍可恢复的 OpenSAC
  session，最终清理由 lease 到期负责。

## 开发检查

```bash
corepack pnpm typecheck
corepack pnpm test
corepack pnpm build
corepack pnpm publint
```

测试只存在于本子包，使用结构化 subprocess fake，不要求运行真实 dsh 或 OpenSAC 服务。
