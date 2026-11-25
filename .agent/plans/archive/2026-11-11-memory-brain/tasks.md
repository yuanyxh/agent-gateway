# 记忆体 v3：类脑记忆架构任务清单

配套方案见同目录 `plan.md`。命令一律在 `agent-gateway` 仓根执行。

- [x] T1 评测基线固化（依赖：无，规模：S）
- [x] T2 schema v3 迁移与回滚（依赖：T1，规模：M）
- [x] T3 稀疏编码与回填（依赖：T2，规模：M）
- [x] T4 覆盖式建边（依赖：T3，规模：M）
- [x] T5 抑制性吸引子提取（依赖：T4，规模：L）
- [x] T6 遗忘曲线与分层（依赖：T2，规模：M）
- [x] T7 实体抽取与 mentions（依赖：T2，规模：M）
- [x] T8 语义事实与时间线（依赖：T7，规模：M）
- [x] T9 双通路检索（依赖：T4、T8，规模：M）
- [x] T10 日序列重放（依赖：T5、T6，规模：L）
- [x] T11 周语义固化与 gist 压缩（依赖：T8，规模：M）
- [x] T12 激活缓冲与启动效应（依赖：T5，规模：S）
- [x] T13 负反馈 LTD（依赖：T6，规模：S）
- [x] T14 生成式推断与审核（依赖：T10、T13，规模：M）
- [x] T15 内存图与快速连跳（依赖：T4、T5、T12，规模：M）

## 检查点 A（T1–T4，MVP）

- [x] 全量测试与 ruff 通过
- [x] 迁移在副本上幂等，回滚可执行
- [x] 孤立率相对 v2 下降 ≥ 40%，固定探针 recall@10 不低于基线

## 检查点 B（T5–T9）

- [ ] 吸引子开关关闭时 100 条固定查询结果逐位一致
- [ ] 开启后 p95 < 150 ms，NDCG@10 下降 ≤ 3%
- [ ] 实体抽取解析失败率 ≤ 5%

## 检查点 C（T10–T13、T15）

- [ ] 连续 7 天重放无超时，heartbeat max < 500 ms
- [ ] 重放新边抽检合格率 ≥ 80%
- [ ] 归档误判率 ≤ 10%
- [x] 从今天的记忆出发，5 跳内到达 5 个月前的记忆（真实数据）
- [x] `memory_expand` 10 跳 p95 < 10 ms，无 SQL、无 LLM

## 检查点 D（T14）

- [x] 推断边全部带审计记录
- [x] 14 天未采纳自动撤销有测试覆盖

---

## T1 评测基线固化

**文件**

- 新建：`scripts/memory_brain_eval.py`
- 修改：`scripts/memory_network_bench.py:120-140`（argparse 增加 `--profile`、`--out`）
- 测试：`tests/test_memory_brain_eval.py`

**接口**

- 消费：`services/retrieval/trace.recent(limit)`、`services/memory/network.stats(db_path)`、`store.readonly_connect(path)`
- 产出：`collect(rows: list[dict]) -> dict`；分层抽样 `sample(strategy="stratified", size=400, seed=20261111) -> list[str]`；命令行输出 JSON 含 `recall_at_10`、`mrr`、`ndcg_at_10`、`isolated_ratio`、`synapse_span_days_p50`、`latency_p95_ms`

**验收**

- [ ] 同一份 trace 与同一快照，连续两次运行指标完全一致（确定性排序，无随机种子）
- [ ] 输出文件写入 `tests/eval/memory_brain/<YYYY-MM-DD>/baseline.json`，缺目录时自动创建
- [ ] trace 为空时输出 `{"status": "empty"}` 而非崩溃
- [ ] 基线含 `isolated_ratio`、`components`、`diameter`，与 2026-11-11 实测对齐（0.6933 / 175 / 27）
- [ ] 同一库、同一种子两次抽样结果完全一致；抽样按月份 × 有边/无边 × 成员数档分层，2026-06 全取

**验证**

- 命令：`PYTHONPATH=src python -m unittest tests.test_memory_brain_eval -v`
- 期望：3 例通过
- 命令：`PYTHONPATH=src python scripts/memory_brain_eval.py --db /tmp/eval.sqlite --out /tmp/baseline.json`
- 期望：退出码 0，JSON 六个指标齐全

**依赖 / 规模**：无 / S

- [ ] 步骤 1：写失败测试，断言空 trace 返回 `status=empty`、两次运行结果相等
- [ ] 步骤 2：运行测试确认失败
- [ ] 步骤 3：实现 `collect` 与 argparse 输出
- [ ] 步骤 4：运行测试确认通过
- [ ] 步骤 5：提交

## T2 schema v3 迁移与回滚

**文件**

- 修改：`src/agent_gateway/services/memory/store.py:26`（`SCHEMA_VERSION = 3`）、`:28-126`（SCHEMA 追加列与表）
- 修改：`scripts/memory_network_rollback.py:20`（`TABLES` 加 4 张表）、`:68-110`（导出带 `schema_version`，缺表跳过并输出 `missing_tables`）
- 修改：utility `Sources/Core/Archive/ConfigArchive.swift:221-225`（表名比较改为只拒绝 incoming 多出的表）
- 新建：`scripts/memory_brain_migrate.py`
- 测试：`tests/test_memory_brain_schema.py`

**接口**

- 消费：`store.connect(db_path)`、`store.readonly_connect(path)`
- 产出：`store.SCHEMA_VERSION`；脚本参数 `--db`、`--dry-run`、`--rollback`、`--force`、`--confirm`

**验收**

- [ ] 在 v2 副本上连续执行两次，第二次输出 `changes=0`，`PRAGMA user_version` 为 3
- [ ] 执行前生成 `memory_graph.v2-<ts>.sqlite` 备份；备份缺失时脚本拒绝 `--rollback`
- [ ] 新增列在旧代码路径下为 NULL/默认值，`store.connect` 不报错
- [ ] 用 v2 时期导出的 JSON 灌进 v3 库：全部表可读、无 NULL 崩溃、`activation_buffer` 为空

**验证**

- 命令：`PYTHONPATH=src python -m unittest tests.test_memory_brain_schema -v`
- 期望：6 例通过（含幂等、备份、回滚、缺备份拒绝）
- 命令：`PYTHONPATH=src python scripts/memory_brain_migrate.py --db /tmp/v2copy.sqlite --dry-run`
- 期望：打印待执行 ALTER 列表，退出码 0，文件 mtime 不变

**依赖 / 规模**：T1 / M

- [ ] 步骤 1：写失败测试，断言 v2 库迁移后 `neurons` 有 `sparse_code`、`user_version=3`
- [ ] 步骤 2：运行测试确认失败
- [ ] 步骤 3：改 `SCHEMA_VERSION` 与 DDL，实现迁移脚本
- [ ] 步骤 4：运行测试确认通过
- [ ] 步骤 5：在 v2 副本上手工跑 dry-run 与真实迁移，核对行数不变
- [ ] 步骤 6：提交

## T3 稀疏编码与回填

**文件**

- 新建：`src/agent_gateway/services/memory/sparse.py`
- 新建：`scripts/memory_brain_backfill.py`（`--sparse` 子命令）
- 修改：`src/agent_gateway/services/memory/network.py:107-165`（`upsert_neuron` 接受 `sparse_code`/`context_key`）
- 测试：`tests/test_memory_sparse.py`

**接口**

- 消费：`numpy.random.default_rng(seed)`、`network.set_neuron_centroid`
- 产出：`encode(vector: list[float], context_key: str, dims: int = 2048, k: int = 64, seed: int = 20261111) -> bytes`；`decode(blob: bytes) -> np.ndarray`；`jaccard(a, b) -> float`；`context_key(episode_id: str, source: str, created_at: str) -> str`

**验收**

- [ ] 同一向量与上下文编码两次字节一致（确定性）
- [ ] 两个明显不同主题的向量 Jaccard < 0.15；同一 episode 内两条 Jaccard > 0.40
- [ ] 生产副本全量回填后 `SELECT COUNT(*) FROM neurons WHERE sparse_code IS NULL AND status='active'` = 0

**验证**

- 命令：`PYTHONPATH=src python -m unittest tests.test_memory_sparse -v`
- 期望：5 例通过
- 命令：`PYTHONPATH=src python scripts/memory_brain_backfill.py --db /tmp/copy.sqlite --sparse --batch 2000`
- 期望：逐批输出 `written`，末行 `missing=0`

**依赖 / 规模**：T2 / M

- [ ] 步骤 1：写失败测试，断言编码确定性与 Jaccard 区分度
- [ ] 步骤 2：运行测试确认失败
- [ ] 步骤 3：实现 `sparse.py` 与神经元写入字段
- [ ] 步骤 4：运行测试确认通过
- [ ] 步骤 5：实现回填脚本并分批复算
- [ ] 步骤 6：提交

## T4 覆盖式建边

**文件**

- 修改：`src/agent_gateway/services/memory/network_build.py:36-51`（`_active_nodes` 去掉 `layer="hot"`，全量 active 记忆入图）、`:396-470`（`candidate_pairs` 锚点条件与复合打分）、`:612-700`（`build_synapse_candidates` 透传 `pair_mode`）
- 修改：`src/agent_gateway/core/config.py:235-285`（新增 `memory_brain_pair_mode()`、`memory_brain_sparse_floor()`）
- 修改：`scripts/memory_network_backfill.py:23-72`（`--pair-mode`）
- 测试：`tests/test_memory_network.py`（追加 4 例）

**接口**

- 消费：`sparse.jaccard`、`network_build.group_centroids`、`config.memory_brain_pair_mode()`
- 产出：`candidate_pairs(..., pair_mode: str = "v2")`；`pair_score(cosine: float, jaccard: float, mode: str) -> float`

**验收**

- [ ] `pair_mode="v2"` 时候选集合与当前实现完全一致（回归测试对比 ids）
- [ ] `pair_mode="v3"` 时单成员神经元进入候选，且 `per_anchor`、`PAIR_LIMIT`、`LLM_PAIRS` 预算不变
- [ ] 稀疏 Jaccard 低于 `memory_brain_sparse_floor()`（默认 0.10）的候选被丢弃
- [ ] cold 层神经元进入候选，tier 不参与入图判定；迁移后孤立率从 69.3% 降到 < 10%

**验证**

- 命令：`PYTHONPATH=src python -m unittest tests.test_memory_network -v`
- 期望：原有全部通过 + 4 例新增通过
- 命令：`PYTHONPATH=src python scripts/memory_network_backfill.py --db /tmp/copy.sqlite --pair-mode v3 --llm-pairs 0 --max-batches 3`
- 期望：三批输出 `pairs>0` 且 `vector_unavailable=false`
- 命令：`PYTHONPATH=src python scripts/memory_network_backfill.py --db /tmp/copy.sqlite --pair-mode v3 --sample 400 --seed 20261111 --llm-pairs 200`
- 期望：VM 规模跑完 < 10 分钟，覆盖 2026-06 至 2026-11 全部月份

**依赖 / 规模**：T3 / M

- [ ] 步骤 1：写失败测试，断言 v2 模式候选不变、v3 模式含单成员锚点
- [ ] 步骤 2：运行测试确认失败
- [ ] 步骤 3：实现全量入图（`_active_nodes`）、条件放宽与复合打分，全部由开关控制
- [ ] 步骤 4：运行测试确认通过
- [ ] 步骤 5：在副本上跑三批 dry-run，核对孤立率变化
- [ ] 步骤 6：提交

## T5 抑制性吸引子提取

**文件**

- 新建：`src/agent_gateway/services/memory/attractor.py`
- 修改：`src/agent_gateway/services/retrieval/arms/associative.py:90-158`（`spread` 后接 `activate`）、`:28-35`（新增常量）
- 修改：`src/agent_gateway/core/config.py:235-285`（`memory_brain_attractor()`、`memory_brain_steps()`、`memory_brain_k()`）
- 测试：`tests/test_attractor.py`

**接口**

- 消费：`network.synapses_of_many(neuron_ids, limit, db_path)`、`config.memory_neuron_activation_floor()`
- 产出：`activate(seeds: dict[str, float], *, db_path=None, steps=10, k=32, alpha=0.35, beta=0.55, gamma=0.10, relative=0.1, restart=0.6, floor=0.02) -> dict`，返回 `{"activated": [{neuron_id, activation, polarity_path}], "rounds": int, "converged": bool, "budget_exhausted": bool}`

**验收**

- [ ] 开关关闭时 `associative_expand` 输出与当前实现逐位一致（100 条固定种子）
- [ ] 单条种子 10 轮内收敛（`converged=true`），结果含至少一个两跳节点
- [ ] 构造 5 跳链 A-B-C-D-E 的测试图，断言 E 的激活 > 0（相对阈值 + 锚点重激发，不被绝对地板截断）
- [ ] 抑制边（`polarity=-1`）降低目标激活，构造图断言 `activation < 无抑制边`

**验证**

- 命令：`PYTHONPATH=src python -m unittest tests.test_attractor -v`
- 期望：5 例通过
- 命令：`PYTHONPATH=src python scripts/memory_network_bench.py --profile v3 --nodes 5000 --neurons 5000 --synapses 100000`
- 期望：`attractor_p95_ms < 150`

**依赖 / 规模**：T4 / L

- [ ] 步骤 1：写失败测试，断言开关关闭时逐位一致
- [ ] 步骤 2：运行测试确认失败
- [ ] 步骤 3：实现带符号邻接表、k-WTA 与收敛判据
- [ ] 步骤 4：运行测试确认通过
- [ ] 步骤 5：在 bench 中加 `--profile v3` 并记录 p95
- [ ] 步骤 6：提交

## T6 遗忘曲线与分层

**文件**

- 新建：`src/agent_gateway/services/memory/forgetting.py`
- 修改：`src/agent_gateway/services/memory/strength.py:52-77`（`apply_decay` 按条衰减）、`:80-103`（`reinforce_memory` 更新 stability）
- 修改：`src/agent_gateway/services/memory/lifecycle.py:70-130`（`classify_tiers` 接 R）
- 测试：`tests/test_forgetting.py`

**接口**

- 消费：`memory_activation` 行、`config.memory_neuron_archive_days()`
- 产出：`retrievability(stability_days: float, elapsed_days: float) -> float`；`next_stability(stability, difficulty, r) -> float`；`initial_stability(importance: int) -> float`；`apply_decay_v3(*, now=None, db_path=None) -> dict`

**验收**

- [ ] `R` 在 `t=S` 时等于 0.9（FSRS 定义），误差 < 1e-6
- [ ] 两次回忆间隔越长、召回时的 R 越低，稳定性增益越大（间隔效应有测试断言）
- [ ] 开关关闭时 `apply_decay` 行为与当前一致（回归测试）

**验证**

- 命令：`PYTHONPATH=src python -m unittest tests.test_forgetting -v`
- 期望：6 例通过
- 命令：`PYTHONPATH=src python -m unittest tests.test_memory_network -v`
- 期望：原有生命周期用例全部通过

**依赖 / 规模**：T2 / M

- [ ] 步骤 1：写失败测试，断言 R(t=S)=0.9 与间隔效应
- [ ] 步骤 2：运行测试确认失败
- [ ] 步骤 3：实现 `forgetting.py` 与批量 SQL 更新
- [ ] 步骤 4：运行测试确认通过
- [ ] 步骤 5：提交

## T7 实体抽取与 mentions

**文件**

- 新建：`src/agent_gateway/services/memory/entities.py`
- 新建：`src/agent_gateway/prompts/zh-Hans/entity_extract.md`、`src/agent_gateway/prompts/en/entity_extract.md`
- 修改：`src/agent_gateway/services/mcp/server_memory.py:769-792`（`_check_contradiction` 输出扩展为 entities/facts）
- 修改：`src/agent_gateway/services/memory/link.py:39-114`（写入时落 mentions）
- 测试：`tests/test_entities.py`

**接口**

- 消费：`prompt_loader.render(name, lang, vars)`、`llm_client.complete(...)`、原始正文
- 产出：`parse_entities(payload: str) -> dict`（`{"entities": [{name,type,aliases}], "facts": [{subject,predicate,object,polarity,valid_from,confidence}], "parse_failed": bool}`）；`extract_batch(memories: list[dict], batch: int = 10) -> list[dict]`；`upsert_entities(connection, memory_id, parsed) -> int`

**验收**

- [ ] 非法 JSON 或字段缺失时返回 `parse_failed=true`，不写任何实体
- [ ] 同一实体不同别名合并为一条（大小写与全半角归一化）
- [ ] 单次写入只增加一次 LLM 调用（与矛盾检测合并），测试用 mock 计数断言
- [ ] 回填每次调用处理 10 条记忆，单条失败不影响同批其他条；全量覆盖 6758 条 active 记忆，含 2490 条未归神经元的

**验证**

- 命令：`PYTHONPATH=src python -m unittest tests.test_entities -v`
- 期望：7 例通过
- 命令：`PYTHONPATH=src python scripts/memory_brain_backfill.py --db /tmp/copy.sqlite --entities --batch 10 --sample 1000 --seed 20261111`
- 期望：输出 `batches≈100 parsed=1000 failed=0 entities>0`

**依赖 / 规模**：T2 / M

- [ ] 步骤 1：写失败测试，断言非法 JSON 不落库
- [ ] 步骤 2：运行测试确认失败
- [ ] 步骤 3：实现解析、归一化与写入
- [ ] 步骤 4：运行测试确认通过
- [ ] 步骤 5：提交

## T8 语义事实与时间线

**文件**

- 修改：`src/agent_gateway/services/memory/entities.py`（新增事实合并与冲突）
- 修改：`src/agent_gateway/services/memory/graph.py`（`memory_edges` 写入带 `valid_from`/`valid_to`/`supports`/`contradicts`）
- 测试：`tests/test_entities.py`（追加 4 例）

**接口**

- 消费：`T7` 的 `facts` 列表
- 产出：`merge_fact(connection, fact: dict, evidence_memory_id: str, now: str) -> str`（返回 `inserted|updated|conflicted`）；`resolve_conflict(connection, subject: str, predicate: str, now: str) -> int`

**验收**

- [ ] 相同（主语，谓词，宾语）二次写入只增 `supports`，不新增行
- [ ] 相反宾语写入时旧行写 `valid_to`，新行 `valid_from` 为当前时间，`contradicts` +1
- [ ] 查询某实体当前事实时，`valid_to IS NULL` 的结果唯一

**验证**

- 命令：`PYTHONPATH=src python -m unittest tests.test_entities -v`
- 期望：10 例通过（累计）
- 命令：`PYTHONPATH=src python -m unittest tests.test_memory_graph -v`
- 期望：既有边写入用例通过

**依赖 / 规模**：T7 / M

- [ ] 步骤 1：写失败测试，断言二次写入只增 supports
- [ ] 步骤 2：运行测试确认失败
- [ ] 步骤 3：实现合并、冲突与时间线
- [ ] 步骤 4：运行测试确认通过
- [ ] 步骤 5：提交

## T9 双通路检索

**文件**

- 新建：`src/agent_gateway/services/retrieval/arms/semantic.py`
- 修改：`src/agent_gateway/services/mcp/server_memory.py:1348-1420`（`_build_recall_pipeline` 增加 semantic Stage，开关控制）
- 修改：`src/agent_gateway/services/retrieval/pipeline.py:36-90`（若需新增 `boost` 字段则一并改）
- 测试：`tests/test_semantic_arm.py`

**接口**

- 消费：`entities`/`memory_edges` 查询、`Query.filters`
- 产出：`SemanticArm(name="semantic", fallback_only=False).retrieve(query, limit, filters) -> ArmResult`，item 带 `match_kind="semantic_fact"` 与 `evidence` 列表

**验收**

- [ ] 开关关闭时 pipeline 阶段数与当前一致（回归测试断言 `[s.name for s in pipeline.stages]`）
- [ ] 开启后语义命中项 `evidence` 非空，且不超过 3 条证据
- [ ] 固定探针集 MRR 相对基线提升 ≥ 5%（记录在评测输出中）

**验证**

- 命令：`PYTHONPATH=src python -m unittest tests.test_semantic_arm -v`
- 期望：5 例通过
- 命令：`PYTHONPATH=src python scripts/memory_brain_eval.py --db /tmp/copy.sqlite --semantic --out /tmp/semantic.json`
- 期望：`mrr` 高于 `/tmp/baseline.json`

**依赖 / 规模**：T4、T8 / M

- [ ] 步骤 1：写失败测试，断言开关关闭时阶段列表不变
- [ ] 步骤 2：运行测试确认失败
- [ ] 步骤 3：实现语义臂与 Stage 接线
- [ ] 步骤 4：运行测试确认通过
- [ ] 步骤 5：提交

## T10 日序列重放

**文件**

- 新建：`src/agent_gateway/services/memory/replay.py`
- 新建：`src/agent_gateway/prompts/zh-Hans/replay_sequence.md`、`src/agent_gateway/prompts/en/replay_sequence.md`
- 修改：`src/agent_gateway/services/memory/consolidate.py:490-552`（`run_daily` 末尾调用重放）
- 测试：`tests/test_replay.py`

**接口**

- 消费：`forgetting.retrievability`、`network.synapses_of_many`、`llm_client`、`replay_runs`
- 产出：`sample_seeds(limit=40, db_path=None) -> list[dict]`；`walk_sequences(seed, hops=(3,5), width=5, db_path=None) -> list[list[str]]`；`async def run_replay(*, budget_calls=40, budget_seconds=1200, db_path=None) -> dict`

**验收**

- [ ] 重放在独立事务内写入，失败回滚且 `replay_runs` 记录 `failed`
- [ ] 达到调用数或时间上限即收尾，返回 `{"sampled","reinforced","stopped":"budget"}`
- [ ] 全程 `await asyncio.to_thread`，事件循环心跳 max < 500 ms（bench 断言）

**验证**

- 命令：`PYTHONPATH=src python -m unittest tests.test_replay -v`
- 期望：7 例通过
- 命令：`PYTHONPATH=src python scripts/memory_network_e2e.py --copy-prod --port 9013`
- 期望：`all_ok`，生产库哈希不变

**依赖 / 规模**：T5、T6 / L

- [ ] 步骤 1：写失败测试，断言预算截断与回滚
- [ ] 步骤 2：运行测试确认失败
- [ ] 步骤 3：实现采样、游走、提示词与写入
- [ ] 步骤 4：运行测试确认通过
- [ ] 步骤 5：在 VM 内用真实 LLM 跑一轮，记录耗时与调用数
- [ ] 步骤 6：提交

## T11 周语义固化与 gist 压缩

**文件**

- 修改：`src/agent_gateway/services/memory/consolidate.py:737-781`（`run_weekly` 增加语义固化段）
- 修改：`src/agent_gateway/services/memory/entities.py`（`consolidate_facts`、`gist_of_group`）
- 测试：`tests/test_semantic_consolidation.py`

**接口**

- 消费：`memory_edges` 事实行、`neurons` 分组
- 产出：`consolidate_facts(connection, *, now, min_support=2) -> int`；`gist_of_group(neuron_id, db_path=None) -> str`

**验收**

- [ ] 支持数 ≥ 2 且无矛盾的事实保留；矛盾未解的事实不晋升
- [ ] gist 写回 C 层节点，原文不删除（只降层）
- [ ] dry-run 不写任何行

**验证**

- 命令：`PYTHONPATH=src python -m unittest tests.test_semantic_consolidation -v`
- 期望：5 例通过
- 命令：`PYTHONPATH=src python -m unittest tests.test_memory_network -v`
- 期望：既有 weekly 用例通过

**依赖 / 规模**：T8 / M

- [ ] 步骤 1：写失败测试，断言 dry-run 零写入
- [ ] 步骤 2：运行测试确认失败
- [ ] 步骤 3：实现固化与 gist
- [ ] 步骤 4：运行测试确认通过
- [ ] 步骤 5：提交

## T12 激活缓冲与启动效应

**文件**

- 新建：`src/agent_gateway/services/memory/buffer.py`
- 修改：`src/agent_gateway/services/retrieval/arms/associative.py:38-87`（`soft_seeds` 合并缓冲激活）
- 测试：`tests/test_activation_buffer.py`

**接口**

- 消费：`activation_buffer` 表
- 产出：`writeback(items: list[dict], *, now=None, db_path=None) -> int`；`read(*, now=None, limit=200, db_path=None) -> dict[str, float]`；`prune(*, now, db_path) -> int`

**验收**

- [ ] 写入后 30 分钟内可读到，超过 TTL 返回空
- [ ] 行数超过 200 时按激活值裁剪，保留最高 200
- [ ] 开关关闭时 `soft_seeds` 结果与当前一致

**验证**

- 命令：`PYTHONPATH=src python -m unittest tests.test_activation_buffer -v`
- 期望：4 例通过

**依赖 / 规模**：T5 / S

- [ ] 步骤 1：写失败测试，断言 TTL 与裁剪
- [ ] 步骤 2：运行测试确认失败
- [ ] 步骤 3：实现 buffer 与 soft_seeds 合并
- [ ] 步骤 4：运行测试确认通过
- [ ] 步骤 5：提交

## T13 负反馈 LTD

**文件**

- 修改：`src/agent_gateway/services/memory/strength.py`（新增 `apply_ltd`，行内含于 `unbind_weak` 之后）
- 修改：`src/agent_gateway/services/memory/consolidate.py:553-636`（`detect_contradictions` 命中后调用）
- 测试：`tests/test_ltd.py`

**接口**

- 消费：`memory_edges` 的 `contradicts`、`synapses.polarity`、`memory_activation.stability`
- 产出：`apply_ltd(connection, *, memory_id: str, now: str, delta: float = 0.05) -> dict`

**验收**

- [ ] 被 supersede 或判定矛盾的记忆，其参与突触权重下降且不低于 0
- [ ] 连续 3 次负反馈后稳定性减半，封底 1 天
- [ ] 无反馈信号时不改变任何权重

**验证**

- 命令：`PYTHONPATH=src python -m unittest tests.test_ltd -v`
- 期望：4 例通过

**依赖 / 规模**：T6 / S

- [ ] 步骤 1：写失败测试，断言无反馈零变化
- [ ] 步骤 2：运行测试确认失败
- [ ] 步骤 3：实现 LTD 与稳定性衰减
- [ ] 步骤 4：运行测试确认通过
- [ ] 步骤 5：提交

## T14 生成式推断与审核

**文件**

- 修改：`src/agent_gateway/services/memory/replay.py`（推断分支）
- 新建：`scripts/memory_brain_review.py`
- 测试：`tests/test_inference.py`

**接口**

- 消费：`replay.walk_sequences`、`replay_runs`
- 产出：`propose_inference(sequence: list[str]) -> dict | None`；`write_inference(connection, proposal: dict, now: str) -> str`；脚本 `--list`、`--accept <id>`、`--reject <id>`、`--expire-days 14`

**验收**

- [ ] 推断边 `inferred=1`、`weight` ≤ 0.35、`relation='inferred'`，且每条都能追到 `replay_runs.run_id`
- [ ] 审核脚本可列出待审、采纳、拒绝，拒绝后边置 `archived`
- [ ] 超过 14 天未采纳自动置 `archived`（测试注入时间断言）

**验证**

- 命令：`PYTHONPATH=src python -m unittest tests.test_inference -v`
- 期望：5 例通过
- 命令：`PYTHONPATH=src python scripts/memory_brain_review.py --db /tmp/copy.sqlite --list`
- 期望：输出待审列表（可为空），退出码 0

**依赖 / 规模**：T10、T13 / M

- [ ] 步骤 1：写失败测试，断言 14 天过期撤销
- [ ] 步骤 2：运行测试确认失败
- [ ] 步骤 3：实现推断、审核与过期
- [ ] 步骤 4：运行测试确认通过
- [ ] 步骤 5：提交

## T15 内存图与快速连跳

**文件**

- 新建：`src/agent_gateway/services/memory/graph_index.py`
- 修改：`src/agent_gateway/services/mcp/server_memory.py`（注册 `memory_expand`）
- 修改：`src/agent_gateway/services/memory/strength.py`（突触写入后增量更新索引）
- 测试：`tests/test_graph_index.py`

**接口**

- 消费：`store.connect`、`network.all_synapses`、`strength.coactivate`
- 产出：`GraphIndex.load(db_path) -> GraphIndex`；`GraphIndex.expand(neuron_id, hops=2, limit=20, budget=500) -> dict`；`GraphIndex.refresh(neuron_ids)`；MCP 工具 `memory_expand(memory_id | neuron_id, hops, limit)`

**验收**

- [ ] CSR 结构（indptr/indices/weights/polarity），20 万节点 / 160 万边内存 < 25 MB
- [ ] `expand` 10 跳 p95 < 10 ms，全程无 SQL、无 LLM；超预算返回 `budget_exhausted`
- [ ] 突触新增或权重变化后 30 秒内可见（增量或惰性重建），测试断言
- [ ] 超过 200 万边时按时间分片，常驻最近 2 年，记 `graph_partitioned`

**验证**

- 命令：`PYTHONPATH=src python -m unittest tests.test_graph_index -v`
- 期望：6 例通过
- 命令：`PYTHONPATH=src python scripts/memory_network_bench.py --profile v3 --nodes 200000 --neurons 200000 --synapses 1600000`
- 期望：`graph_bytes < 25 MB`、`expand_p95_ms < 10`

**依赖 / 规模**：T4、T5、T12 / M

- [ ] 步骤 1：写失败测试，断言预算耗尽返回 `budget_exhausted`
- [ ] 步骤 2：运行测试确认失败
- [ ] 步骤 3：实现 CSR 构建、增量更新与游走
- [ ] 步骤 4：运行测试确认通过
- [ ] 步骤 5：注册 MCP 工具并写 schema 描述
- [ ] 步骤 6：提交

**状态（2026-11-11）**：T1–T15 已实现并在 Lume VM 真实数据上跑通；未勾选项见 plan.md「执行记录」与「检查点」。
