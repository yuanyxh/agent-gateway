# 记忆神经网 v2 任务清单

常量统一放在 `services/memory/network.py`，其他模块不得另写一份。

```python
STRENGTH_HALF_LIFE_DAYS = 30
SYNAPSE_HALF_LIFE_DAYS = 90
ACCESS_REINFORCE = 0.15
PATH_REINFORCE = 0.10
COACTIVATE_REINFORCE = 0.20
ACTIVATION_FLOOR = 0.15
HOP_DECAY = 0.75
MAX_DEPTH = 3
BEAM_WIDTH = 5
MAX_VISITED_NEURONS = 50
MAX_EDGES_PER_NEURON = 8
MAX_MEMBERS_PER_VISIT = 5
ACTIVE_MIN_STRENGTH = 0.45
WARM_MIN_STRENGTH = 0.20
WEAK_IDLE_DAYS = 30
WEAK_IMPORTANCE_MAX = 3
ARCHIVE_IDLE_DAYS = 90
ARCHIVE_RETENTION_DAYS = 180
TRASH_RETENTION_DAYS = 30
UNBIND_WEIGHT = 0.15
UNBIND_DAYS = 60
MAX_SYNAPSE_DEGREE = 20
```

## 阶段一：数据与生命周期

### T1 四表与读写

- [ ] T1 神经元/成员/突触/激活四表与读写（依赖：无，规模：M）

**文件**

- 修改：`src/agent_gateway/services/memory/store.py:25-53`
- 新建：`src/agent_gateway/services/memory/network.py`
- 测试：`tests/test_memory_network.py`

**接口**

- `store.SCHEMA` 新增五张表：

```sql
CREATE TABLE IF NOT EXISTS neurons (
  neuron_id TEXT PRIMARY KEY,
  episode_id TEXT UNIQUE,
  title TEXT,
  summary TEXT,
  centroid BLOB,
  member_count INTEGER NOT NULL DEFAULT 0,
  tier TEXT NOT NULL DEFAULT 'active',
  status TEXT NOT NULL DEFAULT 'active',
  heat REAL NOT NULL DEFAULT 0.0,
  importance REAL NOT NULL DEFAULT 0.0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  metadata TEXT
);
CREATE INDEX IF NOT EXISTS idx_neurons_tier ON neurons(tier);
CREATE INDEX IF NOT EXISTS idx_neurons_status ON neurons(status);

CREATE TABLE IF NOT EXISTS neuron_members (
  neuron_id TEXT NOT NULL,
  memory_id TEXT NOT NULL,
  layer TEXT NOT NULL DEFAULT 'hot',
  weight REAL NOT NULL DEFAULT 1.0,
  role TEXT NOT NULL DEFAULT 'member',
  joined_at TEXT NOT NULL,
  PRIMARY KEY (neuron_id, memory_id)
);
CREATE INDEX IF NOT EXISTS idx_members_memory ON neuron_members(memory_id);

CREATE TABLE IF NOT EXISTS synapses (
  synapse_id INTEGER PRIMARY KEY AUTOINCREMENT,
  from_neuron TEXT NOT NULL,
  to_neuron TEXT NOT NULL,
  weight REAL NOT NULL DEFAULT 0.0,
  base_weight REAL NOT NULL DEFAULT 0.0,
  relation TEXT NOT NULL DEFAULT '',
  directed INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'active',
  last_activated_at TEXT,
  activation_count INTEGER NOT NULL DEFAULT 0,
  decay_rate REAL NOT NULL DEFAULT 0.0,
  reinforce_rate REAL NOT NULL DEFAULT 0.0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  metadata TEXT,
  UNIQUE(from_neuron, to_neuron)
);
CREATE INDEX IF NOT EXISTS idx_syn_from ON synapses(from_neuron);
CREATE INDEX IF NOT EXISTS idx_syn_to ON synapses(to_neuron);
CREATE INDEX IF NOT EXISTS idx_syn_status ON synapses(status);

CREATE TABLE IF NOT EXISTS memory_activation (
  node_id TEXT PRIMARY KEY,
  strength REAL NOT NULL DEFAULT 0.0,
  tier TEXT NOT NULL DEFAULT 'active',
  last_activated_at TEXT,
  activation_count INTEGER NOT NULL DEFAULT 0,
  tier_changed_at TEXT,
  purge_after TEXT,
  metadata TEXT
);
CREATE INDEX IF NOT EXISTS idx_activation_tier ON memory_activation(tier);
CREATE INDEX IF NOT EXISTS idx_activation_purge ON memory_activation(purge_after);

CREATE TABLE IF NOT EXISTS network_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
```

- 神经元：`upsert_neuron(neuron: dict, db_path=None) -> bool`、`get_neuron(neuron_id, db_path=None) -> dict | None`、`list_neurons(tier="", status="", limit=500, db_path=None) -> list[dict]`。
- 成员：`upsert_member(neuron_id, memory_id, *, layer, weight, role, joined_at, db_path=None) -> bool`、`members_of(neuron_id, limit=200, db_path=None) -> list[dict]`、`neuron_of(memory_id, db_path=None) -> str`。
- 突触：`upsert_synapse(link: dict, db_path=None) -> bool`（按 `min/max` 排序去重）、`synapses_of(neuron_id, *, min_weight=0.0, limit=20, db_path=None) -> list[dict]`。
- 激活：`get_activation(node_id, db_path=None) -> dict | None`、`upsert_activation(node_id, patch: dict, db_path=None) -> bool`、`tier_counts(db_path=None) -> dict`。
- 质心：`encode_centroid(list[float]) -> bytes`、`decode_centroid(bytes) -> list[float]`，float32。

**验收**

- [ ] 五张表幂等创建；突触同对写入两次只保留一行且权重更新。
- [ ] `members_of` 与 `neuron_of` 双向一致；`tier_counts` 覆盖 active/warm/archive/trash。
- [ ] 质心 float32 编解码误差 ≤1e-6。

**验证**

- 命令：`PYTHONPATH=src python -m unittest tests.test_memory_network`
- 期望：T1 用例全部通过。

**步骤**

- [ ] 用临时 SQLite 写建表、去重、双向查询、质心编解码失败测试。
- [ ] 运行验证命令确认失败。
- [ ] 加 DDL 与 `network.py` 读写函数。
- [ ] 运行验证命令确认通过。

### T2 迁移与对账

- [ ] T2 `episode_id` 迁移与对账（依赖：T1，规模：M）

**文件**

- 新建：`src/agent_gateway/services/memory/network_build.py`（迁移段）
- 新建：`scripts/memory_network_migrate.py`
- 测试：`tests/test_memory_network.py`

**接口**

- `migrate_network(db_path=None, *, apply=False, now=None, hot=None) -> dict`
  - 分页读 active hot 节点；`episode_id` 非空时 `neuron_id=episode_id`，否则 `neuron_id=f"solo:{memory_id}"`。
  - 初始强度：`0.3 + 0.5 * importance/10 + 0.2 * quality_score(row)`，钳制到 [0,1]。
  - `apply=false` 只统计；`apply=true` 幂等写入 neurons / neuron_members / memory_activation。
  - 返回 `{"nodes", "neurons", "members", "orphans", "mismatch", "applied"}`。
- `reconcile_network(db_path=None) -> dict`
  - 删除幽灵成员；重算 `member_count`；删除空神经元与悬空突触。
- CLI：`--db`、`--dry-run`、`--apply`、`--reconcile`、`--rebuild-centroids`。

**验收**

- [ ] 同一库连续 `--apply` 两次，神经元、成员、激活计数不变。
- [ ] 每个 active hot 节点恰好属于一个神经元；无 `episode_id` 的节点进 `solo:`。
- [ ] 对账后 `member_count` 与成员表一致，空神经元为 0。

**验证**

- 命令：`PYTHONPATH=src python -m unittest tests.test_memory_network && PYTHONPATH=src .venv/bin/python scripts/memory_network_migrate.py --db <副本库> --dry-run`
- 期望：单测通过；dry-run 报告计数与库内真实计数一致。

**步骤**

- [ ] 用临时库构造多成员组、单条组、无 episode 节点，写迁移与幂等失败测试。
- [ ] 运行验证命令确认失败。
- [ ] 实现迁移、对账与 CLI。
- [ ] 运行验证命令确认通过。

### T3 动态强度引擎

- [ ] T3 动态强度引擎（依赖：T2，规模：M）

**文件**

- 新建：`src/agent_gateway/services/memory/strength.py`
- 测试：`tests/test_memory_network.py`

**接口**

- `apply_decay(*, now, db_path=None, batch=5000) -> dict`
  - 从 `network_meta["last_decay_at"]` 取上次时间；无则用 `now` 并写入。
  - 记忆：`s *= exp(-ln2 * days / STRENGTH_HALF_LIFE_DAYS)`。
  - 突触：`w *= exp(-ln2 * days / SYNAPSE_HALF_LIFE_DAYS)`。
  - 同一 `now` 重放不再变化；返回 `{"memory_rows", "synapse_rows", "days"}`。
- `reinforce_memory(node_id, *, delta=ACCESS_REINFORCE, now=None, db_path=None) -> float`
  - `s = min(1.0, s + delta * (1.0 - s))`；`activation_count += 1`；`last_activated_at=now`。
- `reinforce_synapse(synapse_id, *, delta=PATH_REINFORCE, now=None, db_path=None) -> float`
  - `w = min(1.0, w + delta * (1.0 - w))`；`activation_count += 1`；`last_activated_at=now`。
- `coactivate(left_neuron, right_neuron, *, delta=COACTIVATE_REINFORCE, now=None, db_path=None) -> bool`
  - 同对突触存在则强化，不存在则按 `base_weight=0.3` 新建。
- `unbind_weak(*, now, db_path=None) -> int`
  - `weight < 0.15` 且 `last_activated_at` 距今 ≥60 天的 active 突触置 `status=archived`；不物理删除。
  - 返回归档条数。

**验收**

- [ ] 同一 `now` 连续调用 `apply_decay` 两次，第二次 `days=0` 且数值不变。
- [ ] 强化单调递增且有上限 1.0；`activation_count` 同步累加。
- [ ] 半衰期 30 天后记忆强度约为原值 0.5（误差 ≤0.01）。

**验证**

- 命令：`PYTHONPATH=src python -m unittest tests.test_memory_network`
- 期望：T3 用例全部通过。

**步骤**

- [ ] 用固定 `now` 写半衰期、幂等、强化上限失败测试。
- [ ] 运行验证命令确认失败。
- [ ] 实现 `strength.py` 三个函数与 `network_meta` 游标。
- [ ] 运行验证命令确认通过。

### T4 分层与弱信号识别

- [ ] T4 分层与弱信号识别（依赖：T3，规模：M）

**文件**

- 新建：`src/agent_gateway/services/memory/lifecycle.py`
- 测试：`tests/test_memory_network.py`

**接口**

- `classify_tiers(*, now, db_path=None, batch=5000) -> dict`
  - 受保护（`protected=true` 或 importance ≥ `MEMORY_PROTECT_IMPORTANCE`）→ `active`，`purge_after=NULL`。
  - 弱信号：突触度 0、`activation_count=0`、importance ≤3、闲置 ≥30 天 → `archive`，`purge_after=now+180d`。
  - `strength ≥ 0.45` 或闲置 ≤30 天 → `active`。
  - `0.20 ≤ strength < 0.45` → `warm`。
  - `strength < 0.20` 且闲置 ≥90 天 → `archive`，`purge_after=now+180d`。
  - 其余 → `warm`。
  - 返回各 tier 迁移计数。
- `set_tier(node_id, tier, *, now, purge_after=None, db_path=None) -> bool`
  - tier 变化时写 `tier_changed_at`；tier 未变不改 `purge_after`。
- `mark_trashed(node_id, *, now, db_path=None) -> bool`
  - 显式删除入口：`tier=trash`、`purge_after=now+30d`；正文与向量此时不删，由 T5 到期清理。

**验收**

- [ ] 四类样例分别落到 active/warm/archive/trash，受保护记忆永远 active。
- [ ] 同一输入重复调用结果一致；进入 archive 时 `purge_after` 恰为 +180 天。
- [ ] 弱信号判定同时满足四个条件，缺一不归档。

**验证**

- 命令：`PYTHONPATH=src python -m unittest tests.test_memory_network`
- 期望：T4 用例全部通过。

**步骤**

- [ ] 写受保护、弱信号、强度分档、闲置边界失败测试。
- [ ] 运行验证命令确认失败。
- [ ] 实现 `classify_tiers` 与 `set_tier`。
- [ ] 运行验证命令确认通过。

### T5 物理清理

- [ ] T5 物理清理（依赖：T4，规模：M）

**文件**

- 修改：`src/agent_gateway/services/memory/lifecycle.py`
- 测试：`tests/test_memory_network.py`

**接口**

- `purge_expired(*, now, db_path=None, dry_run=False, batch=500) -> dict`
  - 选 `tier in ('archive','trash') AND purge_after <= now` 的节点，按 500 一批。
  - 删除顺序：热层 `_delete_hot(memory_id)`（mem0 API 适配层）→ 冷层 Qdrant point（若有）→ `memory_edges`（双向）→ `neuron_members` → `memory_nodes` → `memory_activation`。
  - 清理后重算受影响 `neurons.member_count`；删除空神经元、悬空突触、空激活行。
  - 记录 `gateway_cache()/memory_network_purge.log`：时间、node_id、tier、purge_after，不写正文。
  - 返回 `{"eligible", "deleted", "failed", "neurons_removed", "dry_run"}`。
- 失败隔离：单条失败计数并继续，记录 id；下次重跑可重入。

**验收**

- [ ] 只删除已到期的 archive/trash；active/warm 与受保护记忆不受影响。
- [ ] 删除后四表、热层、冷层均查不到该 id；成员数与突触无悬空。
- [ ] `dry_run=true` 只返回 eligible，不调用任何删除。

**验证**

- 命令：`PYTHONPATH=src python -m unittest tests.test_memory_network`
- 期望：T5 用例全部通过。

**步骤**

- [ ] 用假 hot 与临时库写失败：未到期不删、受保护不删、删除后无残留。
- [ ] 运行验证命令确认失败。
- [ ] 实现 `purge_expired` 与清理日志。
- [ ] 运行验证命令确认通过。

## 检查点 A（T1-T5 后）

- [ ] 真实库副本迁移 dry-run 与 apply 计数一致，重复 apply 无变化。
- [ ] 强度半衰期、分层、弱信号、清理单测全部通过。
- [ ] 清理只在副本验证；真实库尚未触发。

## 阶段二：网络与联想

### T6 质心候选与 LLM 复核

- [ ] T6 质心候选与 LLM 复核（依赖：T2，规模：M）

**文件**

- 修改：`src/agent_gateway/services/memory/network_build.py`
- 新建：`src/agent_gateway/prompts/zh-Hans/neuron_link.md`
- 新建：`src/agent_gateway/prompts/en/neuron_link.md`
- 测试：`tests/test_memory_network.py`

**接口**

- `build_synapse_candidates(*, db_path=None, since="", hot=None, pair_limit=2000, llm_limit=100, floor=0.56) -> dict`
  - 锚点：成员数 2-8 的神经元；目标：任意非空神经元。
  - 质心缺失时按成员向量重算并写回 `neurons.centroid`。
  - 候选 `pair_id = "syn:<a>|<b>"`，分数为质心余弦；同对去重取最高。
- `async review_pairs(pairs, *, limit, batch_size=10, max_calls=10, llm=llm_client.achat) -> dict`
  - 提示词占位符 `{pairs_text}`，JSON 键 `decisions`。
  - 决策：`pair_id`、`relation`（`same_task|follow_up|cause|reference|none`）、`strength`（0-1）。
  - 非法 id、非法关系、越界强度计入 `parse_failed`/`rejected`。
- `merge_weight(cosine, strength) -> float`：`round(0.5*cosine + 0.5*strength, 3)`。

**验收**

- [ ] 锚点只取 2-8 人的神经元；单例神经元与显式组只作目标。
- [ ] LLM 只认输入 pair；`none` 或 `strength<0.5` 不产出突触。
- [ ] 候选与 LLM 调用数不超过预算，超限标 `truncated`。

**验证**

- 命令：`PYTHONPATH=src python -m unittest tests.test_memory_network`
- 期望：T6 用例全部通过。

**步骤**

- [ ] 用假 hot 与 mock LLM 写候选上限、非法解析失败测试。
- [ ] 运行验证命令确认失败。
- [ ] 实现候选、提示词与解析。
- [ ] 运行验证命令确认通过。

### T7 突触写入与限度

- [ ] T7 突触写入、连边与限度（依赖：T6，规模：M）

**文件**

- 修改：`src/agent_gateway/services/memory/network_build.py`
- 测试：`tests/test_memory_network.py`

**接口**

- `write_synapses(decisions, *, db_path=None, min_weight=0.62) -> dict`
  - 仅写 `final_weight ≥ min_weight`；`base_weight=final_weight`，`weight=final_weight`，`status=active`。
  - 同对按 `min/max` 排序；已存在时取两值最大并刷新 relation。
- `enforce_degree_cap(*, db_path=None, cap=MAX_SYNAPSE_DEGREE) -> int`
  - 每神经元按权重降序保留前 20，其余 `status=archived`；记录归档数。
- `top_synapses(neuron_id, *, min_weight=0.0, limit=MAX_EDGES_PER_NEURON, status="active", db_path=None) -> list[dict]`。

**验收**

- [ ] 低于阈值的决策不写；重复写入不产生重复行。
- [ ] 度数超过 20 时只归档低权重突触，不物理删除。
- [ ] 跨组突触与组内突触使用同一张表与同一套读写。

**验证**

- 命令：`PYTHONPATH=src python -m unittest tests.test_memory_network`
- 期望：T7 用例全部通过。

**步骤**

- [ ] 写阈值、重复、度数上限失败测试。
- [ ] 运行验证命令确认失败。
- [ ] 实现 `write_synapses`、`enforce_degree_cap`、`top_synapses`。
- [ ] 运行验证命令确认通过。

### T8 日/周任务接入

- [ ] T8 日/周任务接入（依赖：T5、T7，规模：M）

**文件**

- 修改：`src/agent_gateway/core/config.py`（`_YAML_FALLBACKS` 与 getter 区）
- 修改：`src/agent_gateway/services/memory/consolidate.py:478-505`、`run_weekly:691-708`
- 测试：`tests/test_memory_network.py`

**接口**

- 配置键：

| 配置 | env | 默认 | getter |
|---|---|---|---|
| `memory.neuron.enabled` | `MEMORY_NEURON_ENABLED` | `true` | `memory_neuron_enabled()` |
| `memory.neuron.build` | `MEMORY_NEURON_BUILD` | `true` | `memory_neuron_build_enabled()` |
| `memory.neuron.maintenance` | `MEMORY_NEURON_MAINTENANCE` | `true` | `memory_neuron_maintenance_enabled()` |
| `memory.neuron.maxDepth` | `MEMORY_NEURON_MAX_DEPTH` | `3` | `memory_neuron_max_depth()` |
| `memory.neuron.beam` | `MEMORY_NEURON_BEAM` | `5` | `memory_neuron_beam()` |
| `memory.neuron.activationFloor` | `MEMORY_NEURON_ACTIVATION_FLOOR` | `0.15` | `memory_neuron_activation_floor()` |
| `memory.neuron.archiveDays` | `MEMORY_NEURON_ARCHIVE_DAYS` | `180` | `memory_neuron_archive_days()` |
| `memory.neuron.trashDays` | `MEMORY_NEURON_TRASH_DAYS` | `30` | `memory_neuron_trash_days()` |
| `memory.neuron.llmPairs` | `MEMORY_NEURON_LLM_PAIRS` | `100` | `memory_neuron_llm_pairs()` |

- `run_daily` 顺序：`govern_daily` → `migrate/reconcile` → `apply_decay` → `build_synapse_candidates` → `write_synapses`。
- `run_weekly` 顺序：`classify_tiers` → `unbind_weak` → `purge_expired` → `enforce_degree_cap` → `VACUUM`。
- 任一阶段开关关闭时跳过并返回 `skipped`。

**验收**

- [ ] 默认开启时 `run_daily`/`run_weekly` 产出 `network` 结果键。
- [ ] `MEMORY_NEURON_ENABLED=false` 时两任务不读写新表。
- [ ] 日任务总时长 ≤15 min，周任务 ≤10 min；超时保留已完成批次。

**验证**

- 命令：`PYTHONPATH=src python -m unittest tests.test_memory_network`
- 期望：T8 用例全部通过。

**步骤**

- [ ] 写开关关闭跳过、默认开启产出、顺序断言失败测试。
- [ ] 运行验证命令确认失败。
- [ ] 加配置与 getter，接入日/周任务。
- [ ] 运行验证命令确认通过。

### T9a 联想种子与传播

- [ ] T9a 联想种子与多跳传播（依赖：T7，规模：M）

**文件**

- 新建：`src/agent_gateway/services/retrieval/arms/associative.py`
- 测试：`tests/test_associative_recall.py`

**接口**

- `soft_seeds(query_vec, *, hot, floor=0.40, limit=5) -> list[dict]`
  - Qdrant 查询软地板 0.40；返回 `{memory_id, neuron_id, score}`；`neuron_id` 经成员表反查。
- `spread(seeds, *, db_path=None, depth=MAX_DEPTH, beam=BEAM_WIDTH, floor=ACTIVATION_FLOOR, visited_cap=MAX_VISITED_NEURONS) -> dict`
  - 每层保留激活值前 `beam` 个神经元；每神经元取权重前 `MAX_EDGES_PER_NEURON` 条 active 突触。
  - `a_next = a_parent * weight * HOP_DECAY * (0.5 + 0.5 * neuron_heat)`。
  - 低于 floor 或超过 visited_cap 停止；返回 `{"activated": [...], "paths": [...], "visited", "truncated"}`。
  - 路径元素：`{memory, neuron, synapse: {from, to, weight}}`。

**验收**

- [ ] 构造 A→B→C 强链与 A→D→C 弱链，强链先到达且激活值更高。
- [ ] 深度超过 3、访问超过 50、单神经元边超过 8 时截断并标 `truncated`。
- [ ] 无向量时抛出的异常由调用方捕获，返回 `vector_unavailable`。

**验证**

- 命令：`PYTHONPATH=src python -m unittest tests.test_associative_recall`
- 期望：T9a 用例全部通过。

**步骤**

- [ ] 用临时图库构造强弱链，写传播顺序与截断失败测试。
- [ ] 运行验证命令确认失败。
- [ ] 实现 `soft_seeds` 与 `spread`。
- [ ] 运行验证命令确认通过。

### T9b 成员打分与路径接入

- [ ] T9b 成员打分与路径接入（依赖：T9a，规模：M）

**文件**

- 修改：`src/agent_gateway/services/retrieval/arms/associative.py`
- 修改：`src/agent_gateway/services/retrieval/arms/recall.py:474-500`
- 测试：`tests/test_associative_recall.py`

**接口**

- `score_members(activated, query_vec, *, min_score, reader_agent, hot) -> tuple[list[RetrievedItem], int]`
  - 每个激活神经元取成员权重前 5；批量取向量；`cosine(query_vec, vector)`。
  - tier 惩罚：warm ×0.85，archive ×0.60，trash 排除；archive 仅在 `include_archived=true` 时进入。
  - `final = min(activation * member_weight * cosine, cosine)`；低于 `min_score` 丢弃。
  - `extra`：`edge="associative"`、`neuron_id`、`activation`、`hops`、`path`、`expanded=True`。
- `associative_expand(query, items, filters=None, *, hot=None) -> list[RetrievedItem]`
  - 开关 `memory_neuron_enabled()` 为假返回 `[]`。
  - 种子 = 直接命中映射的神经元 + `soft_seeds`；传播后打分；去重已知 id。
- `expand_episodes` 合并 `hot + cold + edges + associative` 后统一排序。

**验收**

- [ ] 开关关闭时结果与当前完全一致。
- [ ] 模糊种子可经 2-3 跳到达目标，返回路径长度与激活值。
- [ ] archived 默认不返回；`include_archived=true` 时降权返回。

**验证**

- 命令：`PYTHONPATH=src python -m unittest tests.test_associative_recall`
- 期望：T9b 用例全部通过。

**步骤**

- [ ] 写关闭等价、模糊种子、分层惩罚、路径字段失败测试。
- [ ] 运行验证命令确认失败。
- [ ] 实现 `score_members`、`associative_expand` 并接入 `expand_episodes`。
- [ ] 运行验证命令确认通过。

### T10 召回强化回写

- [ ] T10 召回强化回写（依赖：T9b，规模：S）

**文件**

- 修改：`src/agent_gateway/services/mcp/server_memory.py`（recall handler 结果收尾处）
- 修改：`src/agent_gateway/services/memory/strength.py`
- 测试：`tests/test_associative_recall.py`

**接口**

- `reinforce_after_recall(items: list[dict], *, db_path=None, now=None) -> dict`
  - 对返回的记忆调用 `reinforce_memory`；对 `extra["path"]` 中每条突触调用 `reinforce_synapse`。
  - 同一次响应内同一 id 只强化一次；批量写入，失败只记日志不影响返回。
- recall handler 在结果定稿后经 `asyncio.to_thread` 调用，不阻塞响应。

**验收**

- [ ] 命中记忆 `activation_count +1`，强度按公式上升。
- [ ] 路径上的突触权重上升；未被命中的记忆与突触不变。
- [ ] 强化失败时召回结果完整返回。

**验证**

- 命令：`PYTHONPATH=src python -m unittest tests.test_associative_recall`
- 期望：T10 用例全部通过。

**步骤**

- [ ] 写只强化命中项、失败不断召回、去重失败测试。
- [ ] 运行验证命令确认失败。
- [ ] 实现批量回写并接入 recall handler。
- [ ] 运行验证命令确认通过。

### T11 结果字段与分层惩罚

- [ ] T11 结果字段与分层惩罚（依赖：T9b，规模：S）

**文件**

- 修改：`src/agent_gateway/services/retrieval/types.py:64-96`
- 测试：`tests/test_associative_recall.py`

**接口**

- `RetrievedItem.RESULT_EXTRA_KEYS` 增加 `path`、`hops`、`neuron_id`、`activation`、`truncated`。
- `to_result` 输出路径时裁剪到 3 跳、每跳只留 `memory`/`neuron`/`synapse.weight`。
- 结果 `match_kind` 对联想命中使用 `associated`。

**验收**

- [ ] 联想结果返回 `path`、`hops`、`neuron_id`、`activation`。
- [ ] 路径不含完整正文，只含 id 与权重。
- [ ] 原有直接命中输出不变。

**验证**

- 命令：`PYTHONPATH=src python -m unittest tests.test_associative_recall`
- 期望：T11 用例全部通过。

**步骤**

- [ ] 写字段暴露与裁剪失败测试。
- [ ] 运行验证命令确认失败。
- [ ] 修改 `types.py` 与联想结果构造。
- [ ] 运行验证命令确认通过。

## 检查点 B（T6-T11 后）

- [ ] 合成强链可从模糊种子到达目标，路径权重单调递减。
- [ ] 开关关闭结果与当前一致；开启 p95 增量 ≤300 ms。
- [ ] 强化只影响命中记忆与路径突触。

## 阶段三：治理、查询与资源

### T12 分组治理同步

- [ ] T12 分组治理同步（依赖：T8，规模：M）

**文件**

- 修改：`src/agent_gateway/services/memory/group_governance.py:593-710`
- 修改：`src/agent_gateway/services/memory/network.py`
- 测试：`tests/test_memory_network.py`

**接口**

- `sync_episode_change(memory_ids, old_episode, new_episode, db_path=None) -> dict`
  - 更新成员表；旧神经元空则删除；新神经元不存在则创建。
- `merge_neurons(source, target, db_path=None) -> dict`
  - 成员迁移；突触按对端合并取最大权重；删除源神经元。
- `split_neuron(source, groups: dict[str, list[str]], db_path=None) -> dict`
  - 为每组创建 `ep_` 神经元；成员迁移；突触按成员交集比例分配。
- `_apply_decisions` 在写 `episode_id` 后调用同步；失败回滚 mem0 与图库改动。

**验收**

- [ ] 治理 merge 后成员全部指向目标神经元，源神经元与悬空突触为 0。
- [ ] split 后每组神经元成员与治理结果一致。
- [ ] 同步失败时治理写入回滚，`episode_prev` 保留。

**验证**

- 命令：`PYTHONPATH=src python -m unittest tests.test_memory_network`
- 期望：T12 用例全部通过。

**步骤**

- [ ] 写 merge、split、同步失败回滚失败测试。
- [ ] 运行验证命令确认失败。
- [ ] 实现同步函数并接入 `_apply_decisions`。
- [ ] 运行验证命令确认通过。

### T13 MCP 查询与健康统计

- [ ] T13 MCP 查询与健康统计（依赖：T9b、T12，规模：M）

**文件**

- 修改：`src/agent_gateway/services/mcp/server_memory_graph.py:103-143`
- 修改：`src/agent_gateway/services/mcp/server_tool_schema.py:483-519`
- 修改：`src/agent_gateway/services/mcp/server.py`（`/health`）
- 修改：`src/agent_gateway/locales/{en,zh-Hans}.json`
- 测试：`tests/test_memory_graph.py`

**接口**

- `memory_graph` 的 `edge` 增加 `"synapse"` 与 `"associative"`：
  - `synapse`：返回该节点所在神经元的突触邻居，字段 `group`、`weight`、`relation`、`status`、`member_count`。
  - `associative`：从该节点所在神经元做 `spread`，返回 `activated` 与 `paths`，`limit` 作为访问上限。
- `/health` 增加 `memory_network`：

```json
{
  "neurons": 0,
  "synapses": 0,
  "tiers": {"active": 0, "warm": 0, "archive": 0, "trash": 0},
  "orphan_members": 0,
  "degree_p95": 0
}
```

- `param.memory_edge` 中英文案补充“突触/联想”。

**验收**

- [ ] 传成员节点 id 返回神经元、突触邻居与路径。
- [ ] 原有六类边行为不变；`tools/list` 枚举含 `synapse`/`associative`。
- [ ] `/health` 统计与实际计数一致。

**验证**

- 命令：`PYTHONPATH=src python -m unittest tests.test_memory_graph`
- 期望：单测通过；重启 MCP 后手工调用两种 edge。

**步骤**

- [ ] 写三种 edge、健康统计、原有边回归失败测试。
- [ ] 运行验证命令确认失败。
- [ ] 实现分支、schema、文案与健康统计。
- [ ] 运行验证命令确认通过。

### T14 性能与 10 倍数据压测

- [ ] T14 性能与 10 倍数据压测（依赖：T13，规模：M）

**文件**

- 新建：`scripts/memory_network_bench.py`
- 测试：`tests/test_memory_network.py`

**接口**

- 生成 5 万记忆、5 千神经元、10 万突触的临时库与随机 768 维向量；不写真实库。
- 测量：迁移、日衰减、周分层、`spread`、`memory_graph associative` 的 p50/p95、峰值内存、磁盘增量。
- 断言：p95 不超过预算表；度数上限生效；无全量向量加载（用 tracemalloc 上限）。

**验收**

- [ ] 10 倍数据下日/周任务在预算内完成。
- [ ] 联想检索 p95 ≤300 ms；内存峰值 ≤200 MB。
- [ ] 突触行数不超过神经元数 ×20。

**验证**

- 命令：`PYTHONPATH=src python scripts/memory_network_bench.py --nodes 50000 --synapses 100000`
- 期望：输出预算对照表，全部在阈值内。

**步骤**

- [ ] 写压测脚本与预算断言。
- [ ] 先跑 1 千节点确认脚本正确。
- [ ] 跑 5 万节点，记录结果并调索引/批量。
- [ ] 复跑到预算内。

## 检查点 C（T12-T14 后）

- [ ] 治理 merge/split 后成员与突触一致。
- [ ] 10 倍压测通过，指标写入报告。
- [ ] MCP 两种新 edge 返回可解释路径。

## 阶段四：E2E 与发布

### T15 隔离环境与真实库副本

- [ ] T15 隔离环境与真实库副本（依赖：T14，规模：M）

**文件**

- 新建：`scripts/memory_network_e2e.py`
- 测试：`tests/test_memory_network.py`（副本路径与哈希辅助）

**接口**

- CLI：`--source-home`、`--source-qdrant`、`--copy-prod`、`--port 9013`、`--keep`。
- 行为：`mktemp -d` 建沙箱；复制配置、`memory_graph.sqlite`、Qdrant 目录；设置 `GATEWAY_HOME`、`GATEWAY_CACHE`、`QDRANT_PATH`、`MCP_PORT`；启动 MCP 并轮询 `/health`。
- 安全：复制前记录生产 `memory_graph.sqlite` 与 Qdrant 目录哈希；结束时再次计算并断言未变。
- 输出沙箱路径；`--keep` 保留，默认结束时删除临时目录（移到废纸篓）。

**验收**

- [ ] 沙箱 MCP `/health` 正常，生产库哈希前后一致。
- [ ] 副本可读写，生产目录无新增文件。
- [ ] 未传 `--copy-prod` 时拒绝复制，避免误操作。

**验证**

- 命令：`PYTHONPATH=src .venv/bin/python scripts/memory_network_e2e.py --source-home <生产 GATEWAY_HOME> --source-qdrant <生产 QDRANT_PATH> --copy-prod`
- 期望：打印沙箱路径与 health 结果；生产哈希一致。

**步骤**

- [ ] 写哈希比对与拒绝复制失败测试。
- [ ] 实现复制、启动、轮询、清理。
- [ ] 在真实副本上跑通一次。

### T16a 生命周期 E2E

- [ ] T16a 生命周期 E2E（依赖：T15，规模：M）

**文件**

- 修改：`scripts/memory_network_e2e.py`
- 新建：`tests/e2e/network_fixtures.py`
- 测试：`tests/test_memory_network.py`

**场景**

- [ ] 迁移幂等：连续 apply 两次计数一致。
- [ ] 强度：模拟 30 天衰减约半值；命中后强化上升。
- [ ] 分层：构造 active/warm/archive 样例，断言 tier 与 `purge_after`。
- [ ] 弱信号：无突触、零访问、低重要性、闲置 30 天 → archive。
- [ ] 保护：`protected=true` 永不归档、永不清理。
- [ ] 清理：到期后四表、热层、冷层均无残留；未到期与受保护不受影响。
- [ ] 泛化：模板参数化生成记忆，随机项目名与标题；断言不含任何真实 id。

**验收**

- [ ] 六个场景全部通过，失败时输出复现命令。
- [ ] 清理审计日志行数与删除数一致。
- [ ] 所有断言只依赖不变量，不依赖个人记忆文本。

**验证**

- 命令：`PYTHONPATH=src .venv/bin/python scripts/memory_network_e2e.py --source-home <生产> --source-qdrant <生产> --copy-prod --scenario lifecycle`
- 期望：场景全部 PASS。

### T16b 联想与治理 E2E

- [ ] T16b 联想与治理 E2E（依赖：T15，规模：M）

**文件**

- 修改：`scripts/memory_network_e2e.py`
- 修改：`tests/e2e/network_fixtures.py`
- 测试：`tests/test_associative_recall.py`

**场景**

- [ ] 强链 A→B→C：模糊种子 A 经 2 跳到达 C，路径权重单调递减。
- [ ] 弱链 A→D→C：同等条件下 C 的激活值低于强链。
- [ ] 强化：召回后路径突触权重上升，未命中突触不变。
- [ ] 开关关闭：同一 query 结果与当前实现完全一致。
- [ ] 治理 merge：合并后成员与突触一致，回滚脚本可恢复。
- [ ] 治理 split：拆组后成员与突触按组分配。
- [ ] 泛化：链路由模板生成，断言路径长度、激活单调性，不绑定具体文案。

**验收**

- [ ] 七个场景全部通过；路径样例写入报告。
- [ ] 关闭开关时差分为 0。
- [ ] 治理回滚后 `episode_id` 与成员表恢复一致。

**验证**

- 命令：`PYTHONPATH=src .venv/bin/python scripts/memory_network_e2e.py --source-home <生产> --source-qdrant <生产> --copy-prod --scenario associative`
- 期望：场景全部 PASS。

### T17 L3 报告与人工验收

- [ ] T17 L3 报告与人工验收（依赖：T16a、T16b，规模：S）

**文件**

- 修改：`scripts/memory_network_e2e.py`（报告输出）
- 新建：`tests/eval/memory_network/<YYYY-MM-DD>/report.md`

**内容**

- [ ] 命令、环境、沙箱路径、生产哈希前后对比。
- [ ] 迁移计数、分层迁移表、清理审计计数。
- [ ] 强链/弱链路径样例（id 脱敏为短 id）。
- [ ] 延迟与内存预算对照表。
- [ ] 误删检查：受保护、未到期、active/warm 抽样。
- [ ] 人工验收清单与签字位。

**验收**

- [ ] 报告可由第三人按命令复现。
- [ ] 所有预算项有实测值，无“待测”。
- [ ] 用户确认后进入 T18。

### T18 发布、契约与回滚

- [ ] T18 发布、契约与回滚（依赖：T17，规模：M）

**文件**

- 修改：`src/agent_gateway/core/config.py`、`contracts/env.json`
- 新建：`scripts/memory_network_rollback.py`
- 修改：`README.md` 或 `.agent/status.md`（运维说明）
- 测试：`tests/test_memory_network.py`

**接口**

- 契约导出：`agent-gateway --export-contracts` 含全部 `MEMORY_NEURON_*` 键。
- 回滚脚本：
  - `--disable`：把 `MEMORY_NEURON_ENABLED=false`、`MEMORY_NEURON_MAINTENANCE=false` 写入配置文件。
  - `--export-network`：把四表导出为 JSON（只读），用于审计。
  - `--drop-network`：先要求 `--export-network` 成功，再二次确认后删四张网络表；不属于物理删除记忆。
- 运维前置：部署前 `cp memory_graph.sqlite memory_graph.sqlite.bak-<ts>`，Qdrant 目录同理。

**验收**

- [ ] 契约包含所有新键；宿主两个注入点同步。
- [ ] `--disable` 后重启，召回行为回到 v1。
- [ ] 回滚脚本在副本上验证，不触碰生产。

**验证**

- 命令：`PYTHONPATH=src python -m unittest discover -s tests -t . && ruff format --check src tests scripts && ruff check src tests scripts && agent-gateway --export-contracts`
- 期望：全量测试与 lint 通过；契约键齐全。

## 检查点 D（T15-T18 后）

- [ ] 真实库副本 E2E 全场景通过，生产库前后哈希一致。
- [ ] 物理删除后四表、热层、冷层无残留。
- [ ] L3 报告含命令输出、路径样例、分层迁移表与误删检查。
- [ ] 回滚脚本在副本上可将行为退回 v1。

## 增强档（不在本轮）

- [ ] E1 突触方向性与因果链推理。
- [ ] E2 跨设备记忆网同步。
- [ ] E3 神经元摘要自动生成与可视化面板。
