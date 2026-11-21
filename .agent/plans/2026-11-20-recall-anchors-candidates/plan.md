# recall 结果分层验证（anchors / candidates）

## 目标

在 `lume` VM `utility-env` 内，用宿主真实记忆副本（热层 mem0 + 冷层 Qdrant + 图 + notes + 模型缓存），A/B 验证一个假设：

> 把 recall 结果拆成 `anchors`（已证实命中，可为空）与 `candidates`（未证实入口，带拒绝原因），不再用补位行填满 `results`，可以同时做到「明确查询不脏」和「模糊查询不空」。

本轮只验证，不改生产代码、不部署 wheel、不改生产阈值。

## 现状证据

- 本机 trace 11-20：recall 空率 0%，但返回行中 67% 是 `rerank_fallback`，`weak_match` 占 39%。典型 trace：`dense_hot kept=0`、6 条结果全部由 reranker 判负后复活。
- 复活点：`src/agent_gateway/services/retrieval/pipeline.py:296-306`（`trusted_dropped`）。
- 补位点：`pipeline.py:267-280`（weak/fallback 填满 limit）。
- 返回结构：`pipeline.py:365-386`，handler 透传在 `server_memory.py:1405-1427`。
- 历史结论（记忆 91b07468）：`intent routing` 与 caller keywords 加权都被实验否定，服务端不猜意图。
- 现有可复用脚本：`scripts/e2e_recall_shapes.py`（18 样本 × 4~5 形态）。
- 评测集：`tests/eval/datasets/queries_gold.jsonl`，249 条，字段含 `clarity`（specific / vague / synthetic）。

## 方案

### 实验版数据形态

```text
status: ok | candidates_only | empty
results: anchors，只含同时通过 floor 与 rerank 的行
candidates: 未证实入口，每条带 reason
  - below_floor       低于 floor 的 near-miss
  - rerank_rejected   reranker 判负
  - filter_miss       维度过滤排除
related: 同 episode / 图邻居
```

规则：

1. `results` 不填满 limit；为空是合法结果。
2. `candidates` 最多 `limit * 2` 条，按分数排序，永不进入 `results`。
3. `status`：`results` 非空为 `ok`；`results` 空但 `candidates` 非空为 `candidates_only`；两者都空为 `empty`。
4. 阈值不动：hot 0.616、cold/expanded 0.50、notes_semantic 0.37。本轮只验证结构，不调参。

### 评测口径

| 查询类型 | 指标 | 通过线 |
|---|---|---|
| specific | anchors precision@6（命中 gold relevant ÷ anchors 条数） | 不低于基线 strict 行 precision |
| specific | anchors 空率 | 记录，不设上限 |
| vague | 入口覆盖（anchors ∪ candidates 含 gold relevant 的比例） | 不低于基线 results 覆盖 |
| absent | anchors 条数 | 必须为 0 |
| 全体 | p50 / p95 延迟 | p50 增幅 ≤ 10% |

基线 = 当前 master `f63702e` 的行为；实验 = 同一源码上的分层改动。两者跑同一份数据快照、同一批查询。

## 环境

- VM：`utility-env`（stopped），`~/.local/bin/lume start utility-env`，`lume ssh utility-env "<cmd>"`（默认 lume/lume；失败再向用户要密码）。
- 真实副本（一次性快照，VM 内只读使用）：

| 源 | 大小 | VM 目标 |
|---|---|---|
| `~/Documents/selfdata/database/qdrant_data` | 143M | `~/recall-ab/data/qdrant_data` |
| `~/.config/utility/memory_graph.sqlite` | 128M | `~/recall-ab/data/memory_graph.sqlite` |
| `~/Documents/selfdata/notes` | 274M | `~/recall-ab/data/notes` |
| `~/.cache/fastembed_cache` | 700M | `~/recall-ab/cache/fastembed` |
| `~/.cache/utility/models` | 282M | `~/recall-ab/cache/models` |
| `~/.config/utility/retrieval_thresholds.json` | 134B | `~/recall-ab/config/retrieval_thresholds.json` |
| `~/.config/utility/config.yaml` | 8.4K | 脱敏后 `~/recall-ab/config/config.yaml` |

- 代码：用 `git archive HEAD` 生成基线源码（不含工作区未提交改动），实验源码为复制后改 3 个文件。
- 端口：VM 内 19001（基线）/ 19002（实验），不监听宿主。
- 网络：仅 loopback；不注入 LLM key。
- 缓存/数据全部指向 `~/recall-ab`，不写 VM 的真实 `~/.config/utility`。

## 任务索引

见 [tasks.md](tasks.md)，共 8 个任务、4 个检查点。

## 风险与回滚

| 风险 | 处理 |
|---|---|
| 真实记忆含个人数据 | VM 内仅 loopback，不注入 key；测完删除 `~/recall-ab` |
| 模型 tag 不一致导致 collection 名错位 | 不设 `EMBEDDER_MODEL`，用 config 原值；启动前打印 `index_name` 与本机副本目录名比对 |
| 宿主 git 有未提交 `server_tool_schema.py` | 基线用 `git archive HEAD`，不复制工作区 |
| 传输约 1.1GB | 先检查 VM 内是否已有模型缓存，有则复用 |
| 热层初始化慢 | 启动后先调 `/health` 与一次冒烟 recall，再跑评测 |
| 实验失败 | 只停在 VM 副本；删除 `~/recall-ab`，宿主无改动 |

## 不做

- 不改本仓代码、不提交、不部署 wheel（用户指定：先走临时脚本）。
- 不调生产阈值（用户指定：先验证结构）。
- 不验证 GUI 与 Claude Code 客户端渲染（本轮不涉及）。
- 不做冷层切块、不换嵌入/rerank 模型（历史结论：切块收益约 2pp，成本 4.4 倍）。

## 最脆弱假设

本计划假设 `queries_gold.jsonl` 的相关性标注仍然可用来衡量「入口覆盖」。若大量标注 id 已不在当前副本，则 vague 指标会整体偏低，只能看相对差（基线 vs 实验），不能看绝对值。

## 回滚

```bash
~/.local/bin/lume stop utility-env
~/.local/bin/lume ssh utility-env "rm -rf ~/recall-ab"   # 关机前执行
rm -rf /tmp/recall-ab
```

宿主侧不产生任何持久改动。
