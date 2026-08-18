# 版本发布流程

OpenSAC 使用同一个 `vX.Y.Z` Git 标签发布服务与沙箱容器镜像以及 GitHub Release，不发布或附加
Python package distribution。如果标签与代码中的包版本不完全一致，发布工作流会直接失败。

## 仓库的一次性配置

GHCR 使用工作流临时提供的 `GITHUB_TOKEN`，无需保存 registry secret。第一次发布镜像后，需要在
GitHub Packages 设置中把 `opensac` 和 `opensac-sandbox` 都调整为 Public，未登录用户才能直接拉取。

## 版本与 contract 规则

- `src/opensac/_version.py` 是宿主端包的版本源。
- `packages/opensac-sdk/src/opensac_sdk/_version.py` 是 SDK 的版本源。
- 两者必须完全一致，并使用稳定的 `X.Y.Z` 格式。
- `.env.example`、`compose.env.example` 与 `compose.yaml` 中的正式镜像默认标签必须使用同一版本。
- 只有宿主端与 SDK 的 RPC 边界发生不兼容变化时，才增加 `SANDBOX_CONTRACT`。
- `sandbox/Dockerfile` 的默认 contract 必须与运行时代码一致。

修改版本或 contract 后运行：

```bash
uv run python scripts/release.py
```

每个 pull request 的 CI 也会执行该检查。

## 发布正式版本

1. 同时更新两个 `_version.py` 和版本说明。如果 minor 版本变化，还要更新根目录
   `pyproject.toml` 中兼容的 `opensac-sdk` 版本范围。
2. 执行本地发布检查：

   ```bash
   uv lock
   uv sync --locked --extra dev
   uv run ruff check .
   uv run pytest
   OPENSAC_DOCKER_E2E=1 uv run pytest tests/test_sandbox_docker_e2e.py
   uv build --all-packages --out-dir dist --clear
   uvx --from twine twine check dist/*
   uv run python scripts/release.py --tag vX.Y.Z
   ```

3. 提交版本修改，创建并推送 annotated tag：

   ```bash
   git tag -a vX.Y.Z -m "OpenSAC X.Y.Z"
   git push origin main
   git push origin vX.Y.Z
   ```

4. 如果是第一次发布，在 GitHub Packages 中把两个 GHCR package 调整为 Public。
5. 检查两个 GHCR manifest 和 GitHub Release 源码归档。

`opensac` 与 `opensac-sandbox` 都会发布以下镜像标签：

- `X.Y.Z`：不可变的正式版本，推荐部署时使用。
- `X.Y`：该 minor 分支的最新兼容补丁版本。
- `sha-...`：精确追踪源代码 commit。
- `latest`：最新稳定版本的便捷通道。

沙箱镜像还会额外发布 `contract-N`，指向实现 sandbox contract `N` 的最新版本。发布流程不会构建
或发布本地搜索镜像。

生产环境应固定 `X.Y.Z` 或镜像 digest。不要移动或复用已经发布的标签；发布有问题时应增加补丁版本。
