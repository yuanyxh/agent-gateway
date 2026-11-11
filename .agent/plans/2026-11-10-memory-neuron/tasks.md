# 记忆神经单元 任务清单

常量统一放在 `services/memory/neuron.py`，任务内数值不得另写一份。

```python
NEURON_ANCHOR_MIN_MEMBERS = 2
NEURON_ANCHOR_MAX_MEMBERS = 8
NEURON_PAIR_FLOOR = 0.56
NEURON_PER_ANCHOR = 5
NEURON_PAIR_LIMIT = 2000
NEURON_LLM_PAIRS = 200
NEURON_LLM_BATCH = 10
NEURON_LLM_MAX_CALLS = 20
NEURON_MIN_WEIGHT = 0.62
NEURON_EXPAND_TOP_N = 3
NEURON_EXPAND_MAX_GROUPS = 3
NEURON_EXPAND_PER_GROUP = 2
NEURON_EXPAND_DECAY = 0.98
NEURON_PROMPT_FACT_CHARS = 400
NEURON_RELATIONS = ("same_task", "follow_up", "cause", "reference", "none")
```

## T1 存储与访问层

- [ ] T1 存储与访问层（依赖：无，规模：S）

**文件**

- 修改：`src/agent_gateway/services/memory/store.py:25-53`（`SCHEMA`）
- 新建：`src/agent_gateway/services/memory/neuron.py`
- 测试：`tests/test_neuron_links.py`

**接口**

- `store.SCHEMA` 新增：

```sql
CREATE TABLE IF NOT EXISTS memory_group_edges (
  from_group TEXT NOT NULL,
  to_group TEXT NOT NULL,
  weight REAL NOT NULL DEFAULT 0.0,
  relation TEXT NOT NULL DEFAULT '',
  method TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  metadata TEXT,
  PRIMARY KEY (from_group, to_group)
);
CREATE INDEX IF NOT EXISTS idx_group_edges_from ON memory_group_edges(from_group);
CREATE INDEX IF NOT EXISTS idx_group_edges_to ON memory_group_edges(to_group);
```

- `upsert_group_links(links: list[dict], db_path: str | None = None) -> int`
  - 入参键：`from_group`、`to_group`、`weight`、`relation`、`method`、`metadata`。
  - 存前按 `min/max` 排序成对，冲突时更新 `weight`、`relation`、`method`、`metadata`、`updated_at`。
- `group_links(group_id: str, *, min_weight: float = 0.0, limit: int = 20, db_path: str | None = None) -> list[dict]`
  - 双向匹配 `from_group=id OR to_group=id`，返回 `{group, weight, relation, method, created_at, updated_at, metadata}`，`group` 是另一端。
- `all_group_links(*, min_weight: float = 0.0, limit: int = 5000, db_path: str | None = None) -> list[dict]`
  - 按 `weight DESC` 返回全部关联，供治理候选与离线报告使用。
- `prune_group_links(valid_groups: set[str], *, min_weight: float = 0.0, db_path: str | None = None) -> int`
  - 删除任一端不在 `valid_groups` 或 `weight < min_weight` 的行；按 400 对一批删除。

**验收**

- [ ] 重复初始化不报错，表与索引存在。
- [ ] 同组对写入两次只保留一行且权重更新；`group_links` 从任一端都能查到。
- [ ] `prune_group_links` 只删除失效端点与低权重行，其余行原样保留。

**验证**

- 命令：`PYTHONPATH=src python -m unittest tests.test_neuron_links`
- 期望：T1 用例全部通过。

**步骤**

- [ ] 用临时 SQLite 写失败测试：建表、双向查询、重复写入、prune。
- [ ] 运行验证命令确认失败（ImportError 或表不存在）。
- [ ] 在 `store.SCHEMA` 加表，在 `neuron.py` 实现三个函数。
- [ ] 运行验证命令确认通过。

## T2 组质心与候选生成

- [ ] T2 组质心与候选生成（依赖：T1，规模：M）

**文件**

- 修改：`src/agent_gateway/services/memory/neuron.py`
- 测试：`tests/test_neuron_links.py`

**接口**

- `load_neuron_groups(db_path: str | None = None, *, readonly: bool = False) -> dict[str, list[dict]]`
  - 分页读 `store.list_nodes(layer="hot", status="active")`，跳过空 `episode_id` 与 `generated_by`；返回 `episode_id -> [node]`。
- `group_centroids(groups: dict[str, list[dict]], hot) -> tuple[dict[str, list[float]], int]`
  - 按 100 个 id 一批 `retrieve(with_vectors=True)`，成员向量求均值后 L2 归一化；返回质心与缺失向量组数。
- `candidate_pairs(groups, centroids, *, hot, per_anchor=NEURON_PER_ANCHOR, floor=NEURON_PAIR_FLOOR, limit=NEURON_PAIR_LIMIT) -> dict`
  - 锚点：`ep_` 前缀且成员数 2-8；目标：任意非空组（含显式组与单条组）。
  - 锚点质心经 `query_batch_points(limit=per_anchor + 2)` 取近邻，命中点的 `episode_id` 由节点反查；同组跳过。
  - 返回 `{"pairs": [{"left_group", "right_group", "score", "left_members", "right_members"}], "truncated": bool, "vector_missing": int}`；成对去重取最高分。

**验收**

- [ ] 锚点只取 2-8 人的 `ep_` 组；单条组与显式组只作目标。
- [ ] 同组命中被排除；候选按分数降序且不超过 2000；超限 `truncated=true`。
- [ ] hot 不可用或组无向量时返回空候选并计 `vector_missing`，不抛异常。

**验证**

- 命令：`PYTHONPATH=src python -m unittest tests.test_neuron_links`
- 期望：T2 用例全部通过。

**步骤**

- [ ] 构造假 hot 对象（`vector_store.client.query_batch_points` 返回固定命中）写失败测试。
- [ ] 运行验证命令确认失败。
- [ ] 实现三个函数，常量按文件头定义。
- [ ] 运行验证命令确认通过。

## T3 LLM 复核与解析

- [ ] T3 LLM 复核与解析（依赖：T2，规模：M）

**文件**

- 修改：`src/agent_gateway/services/memory/neuron.py`
- 新建：`src/agent_gateway/prompts/zh-Hans/neuron_link.md`
- 新建：`src/agent_gateway/prompts/en/neuron_link.md`
- 测试：`tests/test_neuron_links.py`

**接口**

- `async review_pairs(pairs: list[dict], *, limit: int = NEURON_LLM_PAIRS, batch_size: int = NEURON_LLM_BATCH, max_calls: int = NEURON_LLM_MAX_CALLS, llm=llm_client.achat) -> dict`
  - 输入行：`pair_id | A: <最多 3 条 fact，每条截断到 400 字并标 truncated> | B: <同上>`。
  - 返回 `{"decisions": [...], "calls": int, "parse_failed": int, "rejected": int}`。
  - 决策键：`pair_id`、`relation`、`strength`；`pair_id` 必须来自输入，`relation` 必须在 `NEURON_RELATIONS`，`strength` 为 0-1 数字。
- `merge_weight(cosine: float, strength: float) -> float`
  - 返回 `round(0.5 * cosine + 0.5 * strength, 3)`。
- 提示词 frontmatter：

```yaml
---
name: neuron_link
placeholders: [pairs_text]
json_keys: [decisions]
---
```

- 提示词正文要求：同任务/同交付目标才判 `same_task`；不确定返回 `none`；只使用输入 `pair_id`；只返回 JSON 对象。

**验收**

- [ ] 非法 JSON、未知 `pair_id`、非法 `relation`、越界 `strength` 均计入 `parse_failed`/`rejected`，不抛异常。
- [ ] `relation="none"` 或 `strength < 0.5` 的决策被丢弃，不进入权重合并。
- [ ] 中英提示词都存在且占位符为 `{pairs_text}`。

**验证**

- 命令：`PYTHONPATH=src python -m unittest tests.test_neuron_links`
- 期望：T3 用例全部通过。

**步骤**

- [ ] mock `llm` 返回合法、非法、未知 id 三类响应，写失败测试。
- [ ] 运行验证命令确认失败。
- [ ] 写中英提示词与 `review_pairs`、`merge_weight`。
- [ ] 运行验证命令确认通过。

## T4 离线评估脚本与报告

- [ ] T4 离线评估脚本与报告（依赖：T1-T3，规模：M）

**文件**

- 新建：`scripts/neuron_links_eval.py`
- 测试：`tests/test_neuron_links.py`

**接口**

- CLI：`--db`（默认 `store.graph_db_path()`）、`--out`（必填）、`--llm-limit`（默认 200，0 表示跳过 LLM）、`--pair-limit`（默认 2000）。
- 只读：SQLite 用 `store.readonly_connect`；Qdrant 只做查询。
- 产出：`candidates.jsonl`、`links.jsonl`、`report.md`。
- 报告必含：组数/锚点数/候选数/截断、`vector_missing`、LLM 调用与解析失败、权重分桶、50 对人工抽检表（`pair_id`、两组摘要、预分、LLM 关系与强度、最终权重、人工判定）、Top20 合并候选。

**验收**

- [ ] 生产库只读运行不写任何文件到图库目录。
- [ ] 报告行数与 `candidates.jsonl`/`links.jsonl` 一致；`truncated` 与 `vector_missing` 有值。
- [ ] `--llm-limit 0` 时仍产出候选与报告，标注 `llm_skipped`。

**验证**

- 命令：`PYTHONPATH=src .venv/bin/python scripts/neuron_links_eval.py --out tests/eval/neuron_links/2026-11-10 --llm-limit 0`
- 期望：三个文件生成；报告含候选数与 50 行抽检表。

**步骤**

- [ ] 用临时库 + 假 hot 写报告字段测试。
- [ ] 运行验证命令确认失败。
- [ ] 实现 CLI、候选/链路输出与报告渲染。
- [ ] 运行验证命令确认通过。

## 检查点 B：人工 gate

- [ ] 用户抽检 50 对并填写 `人工判定`。
- [ ] 准确率 ≥80%、LLM 接受率 ≥40%、Top20 合并候选认可 ≥12 对。
- [ ] 结论写入 `plan.md` 的「批准记录」；不通过则停在 MVP，不执行 T5 及以后。

## T5 日任务增量刷新

- [ ] T5 日任务增量刷新（依赖：检查点 B，规模：M）

**文件**

- 修改：`src/agent_gateway/services/memory/neuron.py`
- 修改：`src/agent_gateway/core/config.py:44-64` 与 getter 区（`memory_governance_apply` 附近）
- 修改：`src/agent_gateway/services/memory/consolidate.py:478-505`
- 测试：`tests/test_neuron_links.py`

**接口**

- 配置键与环境变量：

| 配置 | env | 默认 | getter |
|---|---|---|---|
| `memory.neuronLinks.enabled` | `MEMORY_NEURON_LINKS` | `false` | `memory_neuron_links_enabled() -> bool` |
| `memory.neuronLinks.refresh` | `MEMORY_NEURON_REFRESH` | `true` | `memory_neuron_refresh_enabled() -> bool` |
| `memory.neuronLinks.minWeight` | `MEMORY_NEURON_MIN_WEIGHT` | `0.62` | `memory_neuron_min_weight() -> float` |
| `memory.neuronLinks.llmPairs` | `MEMORY_NEURON_LLM_PAIRS` | `100` | `memory_neuron_llm_pairs() -> int` |

- `async refresh_links(*, db_path=None, since="", pair_limit=NEURON_PAIR_LIMIT, llm_limit=None, dry_run=False) -> dict`
  - 锚点只取 `updated_at >= since` 的成员所在 `ep_` 组（`since` 为空取全部）。
  - LLM 上限取 `llm_limit`，缺省读 `memory_neuron_llm_pairs()`；0 表示只用余弦。
  - 最终权重 ≥ `memory_neuron_min_weight()` 才写入；`dry_run=true` 只返回计数。
  - 每次运行后 `prune_group_links(valid_groups, min_weight=阈值)`。
  - 返回 `{"groups", "anchors", "pairs", "accepted", "written", "pruned", "llm_calls", "parse_failed", "dry_run"}`。
- `consolidate.run_daily` 在 `govern_daily` 之后调用 `refresh_links(since=governance_since, dry_run=...)`，结果键 `neuron_links`。

**验收**

- [ ] `MEMORY_NEURON_LINKS=false` 时 `run_daily` 不写关联表，结果与当前一致。
- [ ] `dry_run=true` 不写表、不 prune，返回计数。
- [ ] 增量只把 `since` 之后更新的组当锚点，但目标组可为全量。
- [ ] 组被合并后旧端点在下一次刷新被 prune。

**验证**

- 命令：`PYTHONPATH=src python -m unittest tests.test_neuron_links`
- 期望：T5 用例全部通过。

**步骤**

- [ ] 写配置默认值、dry_run 不落盘、增量锚点、prune 的失败测试。
- [ ] 运行验证命令确认失败。
- [ ] 加配置键与 getter，实现 `refresh_links`，接入 `run_daily`。
- [ ] 运行验证命令确认通过。

## T6 治理合并候选

- [ ] T6 治理合并候选（依赖：T5，规模：M）

**文件**

- 修改：`src/agent_gateway/services/memory/group_governance.py`（候选、解析、应用区）
- 修改：`src/agent_gateway/prompts/zh-Hans/govern_groups.md`
- 修改：`src/agent_gateway/prompts/en/govern_groups.md`
- 测试：`tests/test_group_governance.py`

**接口**

- `load_group_pair_candidates(db_path=None, *, min_weight, limit=50) -> list[dict]`
  - 读取 `neuron.all_group_links(min_weight=min_weight)`；两端均为 `ep_` 组、成员数各 2-8、且合并后成员数 ≤8、`episode_source` 为 `""`/`auto`。
  - 返回 `{"candidate_id": "group_pair:<a>|<b>", "kind": "group_pair", "groups": [a, b], "members": [...], "score": weight}`。
- `build_candidates(...)` 把组对候选并入 `groups` 之后；与已有候选按组 id 去重。
- `_validate_decision`：`group_pair` 只允许 `keep`/`merge`，`split` 计 `rejected`。
- `_apply_decisions` 的 `merge` 分支：目标组取成员更多者，平票取字典序小者；逐成员写 `episode_id`、`episode_prev`、`episode_source="governance"`、`governance_run`；同步 mem0 metadata；重建两组的 episode 边。

**验收**

- [ ] 低权重、显式组、合并后超 8 人的组对不进入候选。
- [ ] `group_pair` 的 `split` 被拒绝并计数；`merge` 后源组成员全部指向目标组。
- [ ] 合并写 `episode_prev`，`scripts/group_governance_rollback.py --apply` 可恢复。

**验证**

- 命令：`PYTHONPATH=src python -m unittest tests.test_group_governance`
- 期望：新增用例与原有用例全部通过。

**步骤**

- [ ] 用临时库构造两个组与一条高权重关联，写候选与合并失败测试。
- [ ] 运行验证命令确认失败。
- [ ] 实现候选、解析、合并与提示词。
- [ ] 运行验证命令确认通过。

## T7 召回扩展

- [ ] T7 召回扩展（依赖：T5，规模：M）

**文件**

- 修改：`src/agent_gateway/services/retrieval/arms/recall.py:467-500`（新增 `_expand_group_links` 并在 `expand_episodes` 调用）
- 测试：`tests/test_recall_neuron_links.py`

**接口**

- `_expand_group_links(items, query_vec, min_score, reader_agent) -> list[RetrievedItem]`
  - 开关 `config.memory_neuron_links_enabled()` 为假直接返回 `[]`。
  - 种子取 `items[:NEURON_EXPAND_TOP_N]` 中 hot 且带 `episode_id` 的条目。
  - 每个种子取 `neuron.group_links(episode, min_weight=config.memory_neuron_min_weight(), limit=3)`。
  - 每组取关联组前 2 个成员，经 `_hot_points` 批量取向量；过滤 archived、非 active、不可见、已知 id。
  - 分数 `min(seed.score * NEURON_EXPAND_DECAY * (0.5 + 0.5 * weight), cosine)`，低于 `min_score` 丢弃。
  - `extra` 增加 `edge="group"`、`group_link={"group": target, "weight": weight}`、`expanded=True`。
- `expand_episodes`：`combined = hot + cold + edges + group_links` 后统一排序。

**验收**

- [ ] 开关关闭时结果与当前完全一致（同输入同输出）。
- [ ] 每 seed 最多 3 组、每组最多 2 条；超限截断。
- [ ] 低权重关联、archived/superseded、不可见节点不进入扩展。

**验证**

- 命令：`PYTHONPATH=src python -m unittest tests.test_recall_neuron_links`
- 期望：新增用例全部通过。

**步骤**

- [ ] 用假 hot 与临时图库写开关关闭等价、权重过滤、上限、可见性测试。
- [ ] 运行验证命令确认失败。
- [ ] 实现 `_expand_group_links` 并接入 `expand_episodes`。
- [ ] 运行验证命令确认通过。

## T8 MCP 图查询

- [ ] T8 MCP 图查询（依赖：T5，规模：S）

**文件**

- 修改：`src/agent_gateway/services/mcp/server_memory_graph.py:103-143`
- 修改：`src/agent_gateway/services/mcp/server_tool_schema.py:483-519`
- 修改：`src/agent_gateway/services/mcp/server_tool_schema.py` 的 `TOOL_DESCRIPTIONS["memory_graph"]`
- 修改：`src/agent_gateway/locales/en.json`、`src/agent_gateway/locales/zh-Hans.json`
- 测试：`tests/test_memory_graph.py`

**接口**

- `handle_memory_graph` 的 `edge == "group"` 分支：解析节点 → `episode_id`；空则返回 `{"found": true, "group": "", "links": []}`；否则返回：

```json
{
  "found": true,
  "group": "ep_xxx",
  "members": [{"id": "...", "title": "..."}],
  "links": [{"group": "ep_yyy", "weight": 0.71, "relation": "same_task", "method": "centroid+llm", "members": [{"id": "...", "title": "..."}]}]
}
```

- `edge` 枚举加 `"group"`；`param.memory_edge` 中英文案补充“组间关联”。

**验收**

- [ ] 传成员节点 id 返回其组、成员与关联；无 episode 的节点返回空组。
- [ ] 原有六类边行为不变。
- [ ] MCP `tools/list` 中 `memory_graph.edge` 枚举含 `group`。

**验证**

- 命令：`PYTHONPATH=src python -m unittest tests.test_memory_graph`，再重启 MCP 后手工调用 `memory_graph(edge="group")`。
- 期望：单测通过；工具返回组成员与关联。

**步骤**

- [ ] 写无 episode、有组、无关联三类失败测试。
- [ ] 运行验证命令确认失败。
- [ ] 实现分支、schema 与文案。
- [ ] 运行验证命令确认通过。

## 检查点 C（T5-T7 后）

- [ ] 全量测试与 ruff 通过。
- [ ] 沙箱 `dry_run` 不写关联表。
- [ ] 开关开启与关闭各跑同一组 query，关闭结果与当前一致，开启净增益 >0。
- [ ] 召回新增 p95 ≤150 ms；超限时先降 `NEURON_EXPAND_MAX_GROUPS`。

## 检查点 D（T8 后）

- [ ] MCP `memory_graph edge=group` 在部署环境实测。
- [ ] 显式组不出现在合并候选；自动组 merge 后回滚脚本可恢复。

## 增强档（不在本轮）

- [ ] E1 把成员两两余弦回填到 `episode` 边 `weight`，供排序使用。
- [ ] E2 关联强度随访问/共召回动态衰减与增强。
- [ ] E3 冷层组关联。
- [ ] E4 组节点实体化评估。
