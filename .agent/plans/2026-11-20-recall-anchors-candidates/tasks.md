# 任务清单

## T1 VM 与真实副本

- 文件：无（VM 环境）
- 依赖：无
- 步骤：
  1. `~/.local/bin/lume start utility-env`，`lume ls` 记录 IP。
  2. `lume ssh utility-env "sw_vers; python3 --version; ls ~/.cache/fastembed_cache ~/.cache/utility/models 2>/dev/null"`，确认可执行与已有模型。
  3. 若模型缺失：`lume cp` 或 `scp` 传 `~/.cache/fastembed_cache`、`~/.cache/utility/models` 到 `~/recall-ab/cache/`。
  4. 传 `qdrant_data`、`memory_graph.sqlite`、`notes` 到 `~/recall-ab/data/`。
  5. 传 `retrieval_thresholds.json`；脱敏 `config.yaml`（清空 `llm.apiKey`、`server.proxy.providers[].apiKey`）到 `~/recall-ab/config/`。
- 验收：
  1. `lume ssh utility-env "du -sh ~/recall-ab/data ~/recall-ab/cache"` 与源大小同量级。
  2. 副本目录含 `collection`、`mem0_jinazh768`、`memory_graph.sqlite`。
  3. VM 内 `python3 -c "import agent_gateway"` 可用（复用 mcp-venv 或 PYTHONPATH）。
- 验证：`lume ssh utility-env "ls ~/recall-ab/data/qdrant_data"` 输出 `collection mem0 mem0_jinazh768 meta.json`。

## T2 基线服务（19001）

- 文件：VM `~/recall-ab/src-base`（`git archive HEAD` 解包）
- 依赖：T1
- 步骤：
  1. 宿主 `git -C /Users/yuanyxh/Documents/selfdata/projects/agent-gateway archive HEAD | tar -x -C /tmp/recall-ab/src-base`。
  2. 传到 VM `~/recall-ab/src-base`。
  3. 以环境变量启动：`PYTHONPATH=src QDRANT_PATH=~/recall-ab/data/qdrant_data GATEWAY_CACHE=~/recall-ab/cache/gateway MCP_PORT=19001`，模型目录指向 `~/recall-ab/cache`。
  4. 打印实际解析路径（`QDRANT_PATH`、`MEM0_QDRANT_PATH`、embedder cache、rerank dir、notes dir）。
  5. 调 `/health`；调一次 recall 冒烟。
- 验收：
  1. `MEM0_QDRANT_PATH` 末段等于副本里的 `mem0_jinazh768`。
  2. 冒烟 recall 返回 `count > 0` 或 `status` 合理，无异常堆栈。
  3. 日志无 collection 不存在、维度不一致错误。
- 验证：`lume ssh utility-env "curl -s localhost:19001/health"`。

## T3 实验源码（分层）

- 文件：VM `~/recall-ab/src-exp`（T2 源码复制）
- 依赖：T2
- 改动：
  1. `services/retrieval/pipeline.py:264-307`：`results` 只取 rerank 通过且 ID 在 `strict_ids` 的行；把 weak 池、filter fallback、rerank 丢弃行收进 `candidates`，分别标 `below_floor` / `filter_miss` / `rerank_rejected`。
  2. `pipeline.py:309-337`：不再调用 `_fallback_candidates` 填 top；`status` 改为 `ok` / `candidates_only` / `empty`。
  3. `pipeline.py:365-386`：返回 `candidates` 与三态 `status`。
  4. `server_memory.py:1405-1427`：payload 透传 `candidates`，`hint` 改为说明「结果为空不代表没有内容」。
- 验收：
  1. `candidates` 条数 ≤ `limit * 2`，每条有 `reason`。
  2. 同一请求中 `results` 与 `candidates` 的 id 无交集。
  3. `status` 只出现三态值。
- 验证：VM 内 `python3 -m py_compile` 通过；单次 curl 检查 JSON 字段。

## T4 实验服务（19002）

- 依赖：T3
- 步骤：同 T2，换 `src-exp` 与端口 19002。
- 验收：
  1. 同一查询在 19001 与 19002 都返回 200。
  2. 19002 返回含 `candidates`，19001 不含。
  3. 两服务日志无异常。

## T5 A/B 评测脚本

- 文件：`/tmp/recall-ab/eval.py`（自建临时脚本，不放进仓库）
- 依赖：T4
- 步骤：
  1. 读 `tests/eval/datasets/queries_gold.jsonl`，按 `clarity` 分组。
  2. 对每条查询分别调 19001（`recall`，limit=6）与 19002。
  3. 记录：`base_ids`、`exp_results`、`exp_candidates`、`exp_status`、`latency`。
  4. 计算：specific anchors precision、入口覆盖、absent anchors 数、p50/p95。
  5. 输出 `/tmp/recall-ab/report.json` 与逐条 `/tmp/recall-ab/rows.jsonl`。
- 验收：
  1. 脚本对缺失标注 id 不崩溃，覆盖率按可达标注计算。
  2. 同一输入重跑两次，指标差异为 0（除延迟）。
  3. 输出含每条查询的命中 id 列表，可人工抽查。
- 验证：`python3 /tmp/recall-ab/eval.py --gold ... --base http://127.0.0.1:19001 --exp http://127.0.0.1:19002`。

## T6 跑 A/B

- 依赖：T5
- 步骤：
  1. 跑 full 集，保存 report。
  2. 按 clarity 拆 specific / vague 各出一张表。
  3. 对 absent / synthetic 查询单独断言 anchors=0。
- 验收：
  1. 关键指标齐全，无缺失值。
  2. 延迟 p50 同时记录两个版本。

## T7 结论与人工抽查

- 依赖：T6
- 步骤：
  1. 抽 specific 10 条、vague 10 条，打印 anchors 与 candidates 正文摘要。
  2. 判定 candidates 是否真的能作为「入口」。
  3. 结论只回答三件事：specific 是否不脏、vague 是否不空、延迟是否可接受。
- 验收：
  1. 每条结论带数据或样本 id。
  2. 给出「是否值得进入正式实现」的明确建议。

## T8 清理

- 依赖：T7
- 步骤：
  1. 停两个 server。
  2. `lume ssh utility-env "rm -rf ~/recall-ab"`。
  3. `lume stop utility-env`。
  4. 删除 `/tmp/recall-ab`。
  5. 把运行结果写进 `.agent/sandbox.md`。
- 验收：
  1. `lume ls` 中 `utility-env` 为 stopped。
  2. `/tmp/recall-ab` 不存在。

## 检查点

- C1（T2 后）：基线可查询，存储路径正确。
- C2（T4 后）：两服务并存，返回结构差异符合预期。
- C3（T6 后）：指标齐全，数值异常先排查再继续。
- C4（T7 后）：结论明确，再决定是否进入正式实现。
