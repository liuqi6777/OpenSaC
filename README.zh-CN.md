# OpenSAC

**面向研究型智能体的开放、可审计 Search-as-Code 运行时。**

[English](README.md) | [简体中文](README.zh-CN.md)

OpenSAC 让外部智能体用 Python 程序表达搜索策略，而不是连续调用一组固定工具。程序可以批量检索、阅读
正文、过滤与融合候选、结构化抽取、持久化中间状态并提交带引用的输出。OpenSAC 在隔离的 Docker 沙箱
中执行程序，所有特权操作统一由 capability broker 代理。

本项目用于研究一个核心问题：

> 在控制模型、检索后端和评测协议保持一致时，让模型用生成的代码组合搜索原语，相比使用模型可见的
> 工具调用，能否提升质量、上下文效率，或改善延迟—成本权衡？

OpenSAC 实现了公开的
[Search as Code](https://research.perplexity.ai/articles/rethinking-search-as-code-generation)
抽象，但并不试图复刻 Perplexity 的内部搜索引擎。

> [!IMPORTANT]
> OpenSAC 目前是持续开发中的研究原型（版本 `0.4.0`），API、部署契约和研究材料仍可能继续演进。

> [!NOTE]
> **发布状态：**[`v0.4.0`](https://github.com/liuqi6777/OpenSaC/releases/tag/v0.4.0) 已发布，
> GHCR 上的服务镜像与沙箱镜像均可公开拉取且版本一致。推荐使用 Docker Compose 安装。OpenSAC
> 不发布 PyPI 包。

## 为什么使用 OpenSAC

- **可编程检索**：生成的 Python 可以用普通控制流完成批量查询、过滤、关联、排序和证据选择。
- **紧凑的类型化 SDK**：`opensac_sdk` 提供搜索、正文、状态、可选结构化 LLM、用量和引用原语。
- **强化隔离执行**：沙箱程序无法访问网络、服务商密钥、Docker socket 或不受限的宿主机文件系统。
- **上下文解耦**：大规模中间结果保留在工作空间中，只有程序明确打印或提交的数据返回控制模型。
- **可追踪证据**：会话级不透明引用与 broker 签发的段落 locator 将候选结果连接到最终引用。
- **研究级观测**：预算、类型化局部失败、trace、阶段耗时、幂等执行和 worker 生命周期支持可复现 rollout。

## 架构

```mermaid
flowchart LR
    A["外部智能体 / rollout harness"] -->|"通过 POST /exec 提交 Python"| B["OpenSAC API"]
    B --> C["隔离的 sandbox 容器"]
    C -->|"带认证的 Unix-socket RPC"| D["Capability broker"]
    D --> E["网页检索"]
    D -. 可选 .-> F["外部本地检索"]
    D -. 可选 .-> G["Pipeline LLM"]
```

OpenSAC 有意不负责 agent loop。外部控制平面选择模型、生成程序、管理 rollout 并评测答案。每个 rollout
应复用同一个 OpenSAC session，使工作空间文件和不透明引用可以跨轮次使用。后端选择、密钥、重试、
限流和资源约束全部保留在服务端。

默认 Compose 部署只有一个常驻的 `opensac` API/broker 容器，每次执行时再创建短生命周期、无网络的
sandbox 容器。Compose 刻意不包含 `local_search` 服务。

## 使用 Docker Compose 快速开始

公开的 `v0.4.0` 镜像包含 OpenSAC API/broker 和隔离执行沙箱。该部署使用网页检索，并且刻意不启动
可选的本地检索器。

环境要求：Git、支持 Docker Compose 的 Docker Engine 或 Docker Desktop，以及 Serper + Jina
凭证。只有运行仓库中的客户端示例或从源码开发时，才需要 Python 和 `uv`。

### 1. 检出并配置发布版本

```bash
git clone https://github.com/liuqi6777/OpenSaC.git
cd OpenSaC
git checkout v0.4.0
cp .env.example .env
cp compose.env.example compose.env
mkdir -p "$PWD/.opensac"
```

在 `.env` 中设置：

```bash
OPENSAC_API_KEY=replace-with-a-long-random-value
OPENSAC_SEARCH_BACKEND=web
OPENSAC_SERPER_API_KEY=replace-with-serper-key
OPENSAC_JINA_API_KEY=replace-with-jina-key
```

不要提交 `.env`。服务商凭证只保留在 API 容器中，不会传递给生成程序。

在 `compose.env` 中，将 `OPENSAC_CONTAINER_DATA_DIR` 设为 `.opensac` 的绝对路径，并将
`OPENSAC_UID` 和 `OPENSAC_GID` 分别设为 `id -u` 和 `id -g` 的输出。在 Linux 上，将
`OPENSAC_DOCKER_GID` 设为 `stat -c '%g' /var/run/docker.sock` 的输出；在 Docker Desktop 上保留
为 `0`。两个镜像标签都保持为 `0.4.0`。

### 2. 拉取并启动容器

```bash
docker compose --env-file compose.env pull
docker compose --env-file compose.env up -d
docker compose --env-file compose.env ps
curl -fsS http://127.0.0.1:8000/healthz
```

第一次执行程序时，服务会自动拉取相同版本的沙箱镜像。OpenSAC 容器通过挂载 Docker socket 创建
短生命周期、无网络的沙箱容器。Docker socket 权限等同于宿主机级 Docker 控制，请仅使用可信账户
运行该服务栈。

查看日志或停止服务：

```bash
docker compose --env-file compose.env logs -f opensac
docker compose --env-file compose.env down
```

不同平台参数、升级回滚和 systemd 配置见[部署指南](docs/deployment.md)。
本地稠密检索仍可作为外部高级后端使用，详见[本地稠密检索](docs/local-search.md)。

## 执行 Search-as-Code 程序

服务可以继续运行在 Docker Compose 中。由于 Python 客户端没有发布到 PyPI，需要从当前源码检出
安装客户端，并导出与服务端相同的 API key：

```bash
uv sync --locked
export OPENSAC_API_KEY=replace-with-the-same-api-key
uv run python
```

在 Python 提示符中，或把下面代码保存后通过 `uv run python FILE.py` 执行：

```python
import os

from opensac import OpenSAC

program = """
from opensac_sdk import sdk

hits = sdk.search("谁提出了 ReAct prompting 方法？", limit=5)
sdk.output.submit({"hits": [hit.model_dump() for hit in hits]})
"""

with OpenSAC(api_key=os.environ["OPENSAC_API_KEY"]) as client:
    session = client.create_session()
    try:
        result = client.exec_code(session["id"], program)
        print(result["output"])
    finally:
        client.delete_session(session["id"])
```

包含多查询融合、正文过滤、JSONL 持久化状态和段落引用的完整示例见
[examples/research_pipeline.py](examples/research_pipeline.py)。

## 安装与发布状态

| 方式 | 状态 | 适用场景 |
| --- | --- | --- |
| Docker Compose | `v0.4.0` 已可用 | 推荐的预构建部署方式 |
| Git 源码检出 | 可用 | 开发、实验和尚未发布的改动 |

`v0.4.0` 已发布适用于 Linux `amd64` 和 `arm64` 的多架构镜像：

- API/broker 镜像 `ghcr.io/liuqi6777/opensac:0.4.0`；
- 强化执行镜像 `ghcr.io/liuqi6777/opensac-sandbox:0.4.0`。

标签触发的工作流还会更新 `latest`，GitHub 也会为发布版本生成常规源码归档。工作流不会发布或附加
Python package distribution，因此没有 PyPI 版本。

服务镜像和沙箱镜像的版本应保持一致。生产环境应固定不可变版本或 digest，不要依赖 `latest`。

## SDK 接口

生成程序通过 `from opensac_sdk import sdk` 导入单例。

| 命名空间 | 主要操作 | 作用 |
| --- | --- | --- |
| `sdk.search` | `search`、`many`、`fuse_rrf` | 检索并融合候选，同时保留 provenance |
| `sdk.content` | `get_many`、`snippets`、`grep`、`read` | 获取、定位和检查证据 |
| `sdk.llm` | `map`、`map_many`、`extract`、`extract_many` | 可选的 broker 模型调用与 schema 校验抽取 |
| `sdk.state` | JSON/JSONL 与工作空间辅助方法 | 在同一 session 的多次执行间持久化显式状态 |
| `sdk.session` | `usage` | 查看策略统计与剩余预算 |
| `sdk.output` | `submit` | 返回结构化输出并解析可信引用 |

批量操作保持输入对齐，并暴露类型化的逐项失败。空搜索结果属于成功结果。段落引用必须使用正文操作返回的
locator。当前公共契约与迁移说明见 [OpenSAC 0.4](docs/opensac-0.4.md)。

## 智能体集成

OpenSAC 支持三种驱动方式：

1. 自定义 agent loop，通过 HTTP/Python 客户端调用；
2. `opensac agent-run` 配合 CLI 版 Search-as-Code skill；
3. `opensac mcp` 配合 Codex 或 Claude Code 的 MCP skill。

模型可见的公开接口仍只有 `sac_run(code)`。对话绑定、session 创建、lease 续租和状态丢失处理全部留在
适配层。完整配置见[智能体集成指南](docs/agent-integrations.zh-CN.md)或
[英文版](docs/agent-integrations.md)。

## 从源码开发

```bash
git clone https://github.com/liuqi6777/OpenSaC.git
cd OpenSaC
uv sync --locked --extra dev
uv run ruff check .
uv run pytest
```

运行 `uv run opensac serve` 可启动前台源码服务。只有测试尚未发布的 SDK 或沙箱改动时，才需要运行
`uv run opensac build-sandbox`。仓库结构和贡献约定见 [AGENTS.md](AGENTS.md)。

## 文档

| 目标 | 文档 |
| --- | --- |
| 部署或升级 OpenSAC | [部署指南](docs/deployment.md) |
| 连接 Codex、Claude Code、CLI 或自定义智能体 | [智能体集成](docs/agent-integrations.zh-CN.md) |
| 配置可选的本地检索器 | [本地稠密检索](docs/local-search.md) |
| 理解架构和研究边界 | [设计目标与路线图](docs/design.md) |
| 迁移到当前能力契约 | [OpenSAC 0.4](docs/opensac-0.4.md) |
| 运行 rollout worker | [RL environment workers](docs/rl-environment-workers.md) |
| 查看研究指标与 trace | [研究观测](docs/research-instrumentation.md) |
| 发布正式版本 | [版本发布流程](docs/releasing.zh-CN.md) |

## 局限性

- OpenSAC 是研究运行时，不是托管搜索产品，也不是完整的智能体框架。
- 真正的隔离依赖 Docker；宿主机模式示例 runner 不构成安全边界。
- 网页检索的质量、可用性、延迟和成本依赖外部服务商。
- 沙箱只能降低风险；多租户部署仍需宿主机加固、认证、监控、网络控制和文件系统配额。

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
