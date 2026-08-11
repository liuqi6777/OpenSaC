# OpenSAC 0.4 版本说明：收紧 Agent Surface

状态：已实现并通过验收  
发布日期：2026-08-12

## 1. 版本目标

0.4 不再增加搜索或内容原语，而是收紧 sandbox 内 agent 能看到的契约。0.3 引入的 provider
retry、limiter、批内去重、in-flight coalescing、execution cancellation、bounded evidence 和
attempt trace 全部保留；这些机制继续由 host 管理，不进入 agent 的常规控制流。

本版的判断标准是：字段只有在 agent 会据此改变 query、读取、验证、引用或停止策略时，才进入默认
接口与 Skill Pattern；provider 运维、缓存命中、队列时间和 coalescing 归因留在 host session usage
与 capability trace。

## 2. 版本与契约

- OpenSAC 与 bundled SDK：`0.4.0`；
- capability contract：`3`；
- sandbox contract：`5`；
- DeepResearch SAC profile 在首次执行前要求 contract 3，旧 server 会 fail fast；
- 升级后必须重建 sandbox image，旧 contract image 会在执行前被拒绝。

0.4 是一次有意的 breaking release，不同时维护 0.3 的冗余字段。

## 3. Agent-facing 契约变化

### 3.1 Failure 只有一个权威表示

Search 与 content 继续使用 `CapabilityFailure`：

```python
batch.failure.code
batch.failure.message
batch.failure.retryable

passage.failure.code
```

删除：

- `SearchBatch.error`；
- `FusionBatchError.error`；
- `ContentSnippet.metadata["fetch_error"]`。

`FusionBatchError.failure` 现在是必需的 typed failure。失败 search batch 必须为空 hits，失败
content row 必须为空 text 且没有 locator state。旧字段输入会显式校验失败，避免 agent 在新旧
语义之间静默漂移。

### 3.2 Fusion provenance 去掉重复上下文

`CandidateSource` 保留 agent 做 provenance 判断所需的字段：

```text
batch_index, query, backend, rank, score
```

删除 source 上重复的 `request` 与 `retrieval`。代表 hit 仍可携带有效 retrieval metadata，batch
也仍可携带 request information；同一份信息不再复制到每个 source。

### 3.3 Sandbox usage 只保留策略信号

`sdk.session.usage()` 返回：

```text
exec_calls
search_calls
content_fetches
llm_calls
pipeline_model_tokens
documents_seen
budget_remaining
terminal_reason
```

provider attempts、retry、queue/backoff、backend fetch、dedupe、coalescing、cache 和 evidence
registry 使用量仍由 host 完整记录。外部 harness 可以读取它们用于评测和诊断，但 agent 不需要在
正常程序中处理这些字段。

## 4. Search-as-Code Skill 变化

Canonical Skill 与 DeepResearch 实验 prompt 使用同一套 portable workflow：

1. 多 query 搜索并本地 RRF；
2. 用 ref-keyed pool 跨 execution 保存候选；
3. 每个约束分别 `grep_report`；
4. `read` 实际 passage 并验证返回文本；
5. 只有所有约束都有 broker locator 时才提交 citation。

候选池每行只保存：

```json
{"ref": "...", "title": "...", "date": "...", "score": 0.032}
```

`score = max(old_score, candidate.fused_score)`，因此同一 research stage 被重放时不会重复放大
排序信号。RRF 的 query/rank provenance 仍存在于本次 `SearchCandidate.sources`，但不强制持久化。

Evidence 改成 constraint-keyed `evidence.json`，每个约束只保存：

```json
{
  "launch_date": {
    "pattern": "July 30, 2020",
    "ref": "...",
    "text": "actual read passage",
    "locator": {"id": "...", "ref": "...", "kind": "selected_passage"}
  }
}
```

- `pattern` 用于约束变化时使旧 evidence 失效；
- `ref`、`text` 和 `locator` 分别用于答案、验证和 citation；
- locator 只能在签发它的同一 broker session 中跨 execution 复用；workspace 文件本身不能让
  locator 跨 session 生效；
- locator 容量耗尽时可以使用 passage 推理，但不能伪装成 selected-passage citation；
- regex 只能证明文本出现。关系型事实需要关系明确的 pattern 或 checked `extract_many`，不能把
  关键词共现直接当成事实验证。

## 5. 从 0.3.1 迁移

### Search failure

```python
# 0.3.1
if batch.error:
    print(batch.error)

# 0.4
if batch.failure is not None:
    print(batch.failure.code, batch.failure.message)
```

### Content failure

```python
# 0.3.1
error = row.metadata.get("fetch_error")

# 0.4
error = row.failure
```

### Fusion failure

```python
for failed in fusion.batch_errors:
    print(failed.query, failed.failure.code)
```

### Usage

Sandbox 程序只依赖 compact usage。需要 provider attempt、retry 或 coalescing 数据的评测代码应读取
host session usage/capability trace，而不是让生成程序消费这些指标。

### Skill state

旧 `evidence.jsonl` 不会自动迁移。开始新的 rollout，或由 host 显式转换为 constraint-keyed
`evidence.json`。不能把旧 session 的 locator 复制到新 session。

## 6. 明确保留的内部机制

以下能力没有删除，也没有下放给 agent：

- provider error classifier、timeout、retry、backoff 和 `Retry-After`；
- endpoint/credential/operation 级 concurrency 与 RPS limiter；
- search/content 批内精确去重；
- 默认关闭的 session 内 in-flight coalescing；
- execution abort、timeout、output-limit 和 shutdown cancellation；
- evidence registry 容量、collision 与 citation binding；
- logical/provider 双口径 usage 和无正文 capability trace。

`grep` 与 `grep_report` 也保持分工：前者是隐藏 partial fetch failure 的便利接口，后者用于 coverage
敏感的 agent workflow，能区分零匹配与逐输入失败。

## 7. 验收

本版通过：

- OpenSAC 全量测试：297 passed，1 个 opt-in Docker E2E skipped；
- DeepResearch SAC prompt 测试：65 passed；
- Ruff、lockfile、diff check 与 Skill validator；
- Skill 前向测试覆盖多约束、跨 execution evidence、read locator citation、重复 stage 和空 pool。

Docker E2E 仍需在 Docker host 上设置 `OPENSAC_DOCKER_E2E=1`，验证 contract 5 image。
