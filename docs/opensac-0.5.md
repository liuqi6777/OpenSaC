# OpenSAC 0.5 版本说明：Web 段落级证据检索与重排

状态：已实现并通过本地验收
版本日期：2026-08-19

## 1. 版本目标

0.5 在 Web 搜索召回与正文抓取之间补上跨文档 passage 检索层。它解决的核心失败模式是：搜索已经
找到正确网页，但旧 `content.snippets` 的单页单窗口没有展示答案所在段落。

旧的 `snippets`、`grep`、`read` 和 `get_many` 保持兼容；新程序优先使用：

```text
search.many -> search.fuse_rrf -> content.passages -> 检查原文 -> submit locator
```

`grep` 与 `read` 继续负责精确字符串验证和上下文滚动。

## 2. 版本与契约

- OpenSAC 与 bundled SDK：`0.5.0`；
- capability contract：`4`；
- sandbox contract：`6`；
- 升级后必须重建 sandbox image，旧 contract image 会在执行前被拒绝。

## 3. SDK 契约

```python
report = sdk.content.passages(
    query,
    refs,
    limit=20,
    max_per_ref=3,
)
```

- `query` 必须非空；`limit=1..100`；`max_per_ref=1..10`；
- refs 仍受 deployment 的 content ref 上限约束，默认 256；
- 空 refs 和零 passage 都是成功结果；
- 重复 refs 按首次出现位置去重，`input_count` 与 `unique_ref_count` 同时报告；
- `report.failures` 保留抓取失败的原输入位置，其余网页继续参与排序；
- `rank` 是从 1 开始的全局名次，`score` 只保证在同一次调用内可比较。

每个 `ContentPassage` 包含来源元数据、精确正文、ranker、分数和 locator。坐标使用 1-based 行号与
0-based 行内字符位置，end position 尾端排他。

## 4. 检索与重排

Broker 将换行统一为 `\n` 后，以 2,000 字符窗口和 200 字符重叠确定性切块。切点优先选择后半段的
段落或换行边界；长单行没有边界时硬切，坐标仍可精确映射回规范化正文。

所有窗口先用支持英文词项和逐字中文 token 的 request-local BM25 预筛：每个 ref 先保留
`max(8, max_per_ref)` 个窗口，再从全局最多 100 个候选中排序。最终排序后才应用 `max_per_ref`，同分
按输入 ref 顺序和 passage 坐标稳定排序。

默认 ranker 是本地 `lexical:bm25`，不会增加网络请求：

```dotenv
OPENSAC_PASSAGE_RANKER=lexical
```

Jina reranker 是显式 opt-in；候选 passage 会发送给外部服务：

```dotenv
OPENSAC_PASSAGE_RANKER=jina
OPENSAC_JINA_API_KEY=replace-with-jina-key
OPENSAC_PASSAGE_RERANKER_MODEL=replace-with-explicit-model
```

Jina 请求通过统一 provider runtime，因此复用 timeout、safe retry、`Retry-After`、并发、RPS limiter
和 attempt trace。缺少 key/model、429/5xx 最终失败或非法响应都会返回 typed error，不会退回 lexical。

## 5. Evidence 与 trace

只有最终返回的 passage 会进入 evidence registry。容量耗尽时 passage 仍返回，但 `locator=None` 且
`locator_error.code == "evidence_capacity_exhausted"`；没有 locator 的 passage 不能作为正文 citation。

`CapabilityEvent.passage_records` 只记录 document identity、ref、ranker、rank、score、坐标和 passage
fingerprint，不记录正文。Jina request/response body 同样不会进入 error 或 trace。

## 6. 验收范围

仓库测试覆盖参数边界、ref 去重、稳定切块/排序、中英文 BM25、长单行、partial fetch failure、locator
解析、evidence 容量、旧 API 兼容，以及 Jina 索引映射、乱序结果、重复/越界索引、缺配置、429/5xx
retry、脱敏 trace 和禁止静默降级。

冻结网页与 gold span 组件集同时计算旧 snippets 与新 passages 的 Recall@5、MRR 和 locator 可解析率，
并要求新能力的 Recall@5 与 MRR 点估计更高。

BrowseComp accuracy 与 WideSearch row-level F1 的交错端到端实验仍属于发布评测步骤：必须固定模型、
reasoning、预算、题集和单轨迹协议，同时报告 p50/p95 延迟、抓取/重排失败率和 provider 成本。仓库内
单元与组件测试不伪造这些需要外部数据、模型和凭证的结果。
