# 项目状态

## 当前目标

完成公开发布准备：双语 README、MIT 许可证与打包元数据；提交与推送由用户决定。

## 进行中

| 任务 | 状态 | 阻塞 |
| --- | --- | --- |
| G2 宿主接入 | wheel 分发与模块启动已在 utility 侧验证；等本仓改动提交 | — |
| 记忆 episode_id 分组 | 阶段 1/2/3 完成；9 个维度写入/查询已落地并通过审查修复，598 例通过；泛化评估自动分组组纯度 67.9%，已回退，分组仍只靠显式 episode_id | — |
| 检索层重构 | 通用协议 + 管线落地，recall 已迁移；250 例通过；固定快照 100/100 一致；线上 143 条 0 错误；开关实测默认关闭最优 | — |
| 评测集标注 | **已停用**：query 由历史会话关键词提取，与用户当时意图不对应（用户判定），基于 query 的相关性标注不成立；改用真实使用 case 驱动排查 | — |
| 换模型 jina-zh | 热层/冷层已重建，地板由自动标定写入（现 hot 0.616 / cold 0.569）；可采信的结论只有不相关率：minilm 40.5% → jina 12.5%（249 条同批、不依赖意图判断） | — |
| notes 层降噪 | 语义复核（minilm 0.40 / jina 0.35）+ **仅作兜底**：hot/cold 有命中时 notes 不进主结果；抽样 100 条 notes 占比 26.2%→8.5%，空返回不增 | — |
| 记忆体第二轮改进 | 近邻矛盾候选、打分批量写、受保护记忆、dry-run、检索层归档过滤、多信号排序（默认关）、摘要预算折叠已实现并通过 379 例；等打包部署 | — |
| gateway 与 utility 完全解耦 | 配置/路径/项目数据/图库写权已落地；legacy 适配保留一个发布周期；522 例与 ruff 通过；待用户验证与提交 | — |
| 记忆分组治理 | 已接入日任务：增量候选、默认应用 split/merge、写 `episode_prev`/`episode_source`、12 次调用预算；628 例通过 | 待重启 MCP 生效；离线报告仍全量运行 |
| 记忆神经网 v2 | T1-T18 完成；2026-11-11 两仓 review 修复 11 项与三观察项（事件循环阻塞、readonly、时区锚点、治理断边、配置嵌套），724 例与 ruff 通过；Lume VM 复验 daily/weekly 心跳 max 204/326 ms、`llm_skipped=false`、共激活建边 13（证据 `tests/eval/memory_network/2026-11-11/vm-fixes/`） | 待重新打包 utility；关联臂召回质量尚无前后对比；治理 split/merge 的 seed 重建未端到端 |

## 已完成

- 2026-10-26 基座生成并由用户确认（.agent、6 个技能、契约、CI）
- 2026-10-26 从 utility 仓迁入 Python 能力层，import 改为 `agent_gateway`；`PYTHONPATH=src python -m unittest discover -s tests -t .` → 128 例通过（1 skipped）
- 2026-10-26 `agent-gateway --check` 在真实与临时配置下通过
- 2026-10-26 wheel 构建与干净 venv 安装验证通过，资源（prompts / ast-js）指向 site-packages
- 2026-10-26 `--export-contracts` 产出 `contracts/env.json`（39 个 env 键）
- 2026-10-26 采用 src layout（`src/agent_gateway/`），pyproject 与测试路径同步
- 2026-10-26 lint 基线清零（641 → 0）：删除未使用导入，兼容再导出模块加 per-file-ignores，CI lint 转阻塞门
- 2026-10-26 宿主接入契约落地：`server.py` 的 `async main` 拆成同步 `main()` + `async serve()`（模块名启动 `main()` 原来只拿到未 await 的协程，进程秒退）；`scripts/server.py` 同步
- 2026-10-26 迁入两个检查脚本 `scripts/check-web-search.py`、`scripts/check-search-cache-invariants.py`（import 改 `agent_gateway`，E402 按文件豁免）
- 2026-10-28 解耦 P1-a：新增通用 `summarize` MCP 工具（单次文本总结，复用 `llm/client`，输入上限 50k 字符）；契约扫描登记 `_json_env`，导出含 `GATEWAY_SEARCH_*`
- 2026-10-28 解耦 P0b：新增 `services/search/engine_config.py`，引擎 spec/顺序/限速数据化（内置默认 + `search.*` 配置 + `GATEWAY_SEARCH_*` env）；Google 改为 `search.providers.google.command` 注入，删除 `utility_binary()` 路径嗅探与 `UTILITY_BIN`；无 provider 时 Google 自动从顺序中移除
- 2026-10-28 解耦 P0a：新增 `core/paths.py`（`GATEWAY_HOME`/`GATEWAY_CACHE`），`GATEWAY_CONFIG` 指定配置文件，data_dir/日志/agent_debug/fastembed 统一走 paths；过渡期兼容 `UTILITY_DATA_DIR` 与旧 cache 目录
- 2026-10-28 解耦 P2：i18n 语言包自持（`src/agent_gateway/locales/{en,zh-Hans}.json`，57 键），`core/l10n.py` 启动加载 JSON，删除生成的 `core/l10n_catalog.py`；宿主 `gen-l10n.py` 去掉 `--python-out`，`check-l10n-contract.py` 只校验 Swift 侧，宿主 JSON 移除 57 个 mcp/proxy 键（203→146）
- 2026-10-28 解耦 P1：RSS 日报编排迁宿主（utility `RSSDigestService` + `rss --digest`），删除 `services/rss/*`、`scripts/rss_daily.py`、`rssDaily` 入口、`RSS_DAILY_CONFIG` env 与 rss 提示词；新增 REST `/api/summarize`、`/api/read`、`/api/llm/chat`，宿主经 HTTP 调用通用能力、自持业务提示词
- 2026-10-28 episode_id 阶段 3：冷层写入按内容最相似的源记忆继承分组（无向量时退化为多数派）；recall 对 top-3 冷层命中按组扩展（每组 ≤3，系数 0.98，过滤 stale/archived）；新增 8 例测试
- 2026-10-28 修复冷层分组跨主题误分：线上实测发现 5 条不同主题摘要共享 `ep_gateway_decouple_p2`，改为按摘要向量与源记忆向量取最相似者继承，并加 0.45 相似度下限（低于则新建组）
- 2026-10-28 抓页噪声治理：`article.py` 接入 trafilatura（空/短结果回退 `html_to_markdown`），websearch 丢弃 <200 字符页面、默认 `fetch_top=0`，boilerplate 规则补 related/recommend/rank 类名；结果排序对空/短摘要（<20 字符）降权 0.15
- 2026-10-29 补 `set_memory_status` 生命周期测试（`tests/test_memory_status.py`，3 例）：恢复 active 清 `deleted_at` 与 supersede 字段、只清 retired 位保留 summarized 位；删除置 `deleted_at` 并置 retired 位
- 2026-10-29 规则注入改确定性位置策略：首条真实 user 注入 full 块（`--rule_start`），其余注入内置英文短提示（`--short_rule_start`）；`MODEL_PROXY_RULE_INJECTION` 控制 `mode` 与 `fullEvery`；剥离兼容旧三横线块；已注入判定改为前缀匹配，避免正文提及标记时误跳过；新增 12 例测试
- 2026-10-31 回退上述策略：每条真实 user 消息重新注入完整规则块（用户实测短提醒遵从率下降）；删除 `modelProxy.ruleInjection` 配置与 `MODEL_PROXY_RULE_INJECTION` env；剥离仍兼容历史三横线与短块；测试类改为 `InjectionTests`，全量 273 例通过
- 2026-10-29 按 agent-vibe 1.0.0 升级基座：补 `seed_version` 与 `memory/INDEX.md`、`pre-commit`/`commit-msg`、PR 模板、core 标记；`core.md` 拆为 5 个主题规则，迁入 engineering / testing / vcs / design / collaboration / documentation / library 规则
- 2026-10-29 工具面收敛：移除 MCP `project_dependencies` / `project_exports` / `summarize` 与对应 handler/schema/tests；`summarize` 仅保留 REST；`project_notes` 增加混合重排、MMR 去重、扫描后预热（`project_search`/`project_structure` 触发 + `/api/project/notes/prewarm`）；新增 3 例缓存清理与分块测试，测试 189 例通过；wheel 已部署复验（合成项目触发 `pruned 1 stale files`）
- 2026-10-29 工具统计修复：`api_usage` 新增 `tool_names`（每次响应全部工具，JSON 数组），流式收集全部 `content_block_start` / `tool_calls`，非流式从响应体解析；`tool_output_log` 按 `tool_call_id` 去重（真实副本 9168→8498 行）；新增 `GET /usage/tools`，旧行按单工具计入 `legacy_calls`；测试 197 例通过；已部署复验（代理 23:37 重启，新行 `["Bash","Bash","Bash"]`，线上 `tool_output_log` 8502=8502）
- 2026-10-30 检索质量修复：`catch_up` 新增 `limit`/`offset`/`max_chars`（默认 50/0/24000），返回 `returned`/`truncated`/`next_offset`；`read_url` 对 400/404/410 直接终止，失败增加 `failure_kind`；`project_structure` 增加 `status`（ok/empty/path_not_found/not_scanned）；`project_search` 结果补充 `module`/`importedBy`/`importCount`，向量-only 结果降级为 `weak_match` 并按词法锚点过滤，中文向量-only 直接 `no_match`，旧符号表缺 `start_line` 时回退无行号查询；新增 13 例测试，全量 213 例通过
- 2026-10-30 recall 精度修复：热层 `threshold=0.68`、冷层 Qdrant `score_threshold=0.56`；episode 扩展按实际相似度过滤并取 `min(seed*0.98, actual)`，低于 0.56 丢弃；notes 改为 ASCII 词 + 中文 bigram 覆盖率 ≥60%；主结果只保留 direct，扩展进入 `related`，新增 `status`（ok/weak_match/no_match）与 `match_kind`；冷层/扩展分数统一转 Python `float`，修复部署后 `float32 is not JSON serializable`；新增 10 例测试，全量 223 例通过；重新部署后 141/141 成功，负例 `ok` 0，历史旧低分 36→0，旧 expanded 47→related 49
- 2026-10-30 检索层重构（P1-P3）：新增 `services/retrieval/`（types/protocol/registry/pipeline/text/fusion/gate/trace/observe/config）；recall 迁到三臂（dense_hot / dense_cold / lexical_notes）+ HeatWeightedFusion + LayerPresenceGate，行为保持等价；project_search/project_notes 加 trace 装饰器；新增 `GET /api/retrieval/stats`；模型描述与阈值按 model_tag 分档（jinazh768 未标定即报错）；多子查询、RRF、相对门控由 `RETRIEVAL_REWRITERS` / `RETRIEVAL_GATE` 开关控制，默认关闭；新增 21 例检索层测试，全量 244 例通过
- 2026-10-30 检索层方案收敛：删除未实现/实测否决的组件 —— `expand/term_split.py`（bigram 拆词无效果，延迟 5.7 倍）、`registry.py`（无调用方）、`protocol.Router`/`QueryRewriter`/`Reranker`、`types.RouteDecision`/`PipelineResult`、`Pipeline.reranker`/`corrector`/`rewriters`、`Query.sub_queries`；reranker 实测（jina-reranker-v2）内存 +3.4GB 且排序劣于 embedding，corrector 实测（LLM 改写）4 条 no_match 救活 0 条且改坏 2 条；notes 改为按需执行，普通查询延迟 945ms→132-209ms；全量 265 例通过
- 2026-10-30 换模型 jina-zh 并修热层空结果：`HotMemoryArm` 改用 `score_details.semantic_score`（mem0 混合分与余弦阈值不可比）；`_build_recall_pipeline` 全部阈值改读 per-model 表（原先 hot/cold 用常量，换模型无法标定）；新增 `POST /api/retrieval/rebuild`、`/api/retrieval/diagnose`；三臂阈值标定并把实测值写进 `FLOORS["jinazh768"]`、清空 `SEEDED_TAGS`；新增 `scripts/export_judge_batch.py`；盲标 249 条 × 3 轮，全量 261 例通过
- 2026-11-01 记忆图谱与生命周期：新增 `services/memory/{store,graph,link,retention,consolidate}.py`（节点/边 SQLite、六类边、质量分档保留、日/周整合）；`memory_get`/`memory_graph` 工具；`remember` 拆分 fact/context 并新增 type/source_files/source_url；冷层 point id 改 uuid5；归档改软删；`scripts/migrate_memory_graph.py` 回填与停服迁移脚本；全量 348 例通过
- 2026-11-01 记忆体第二轮改进：周任务矛盾候选由全量矩阵改为 Qdrant 近邻（`query_batch_points` 分批 top-k，失败回退矩阵）；`score_relevance` 改单事务批量写；新增 `importance`/`protected` 与 `MEMORY_PROTECT_IMPORTANCE`，矛盾与归档跳过受保护项；周任务与 `/api/consolidation/run` 支持 `dry_run`；`recall` 检索层用 mem0 `NOT archived=true` 排除归档；新增 `SignalWeightedFusion`（recency/access/quality 默认权重 0）；周任务后 `VACUUM`；删除死常量 `HOT_RETENTION_DAYS`、`SUPERSEDED_RETENTION_DAYS`；新增 13 例测试，全量 369 例通过
- 2026-11-01 多信号排序补数据链路：热臂 `extra` 补 `heat_score`/`access_count`/`last_accessed_at`，`relevance` 由图库回填；`recall` 对热层 top 结果 `access_count += 1`；100 条 known-item 评测（`scripts/eval_rank_signals.py`）显示只有 recency 0.2 略有提升（hit@1 0.69→0.72），默认权重保持 0
- 2026-11-01 摘要预算折叠：日任务末尾按 `MEMORY_MAX_SUMMARIES` / `MEMORY_MAX_SUMMARIES_PER_EPISODE` 折叠超预算摘要（保留最新 `cap-1`，其余合并为一条 `level+1`，上限 3 层），旧摘要软归档并写 `derived_from`；预算为 0 时不折叠；新增 4 例测试；全量 379 例通过
- 2026-11-01 跨仓配置链打通：`MEMORY_*` / `RANK_SIGNAL_*` 写入 `_YAML_FALLBACKS`（selfManaged 回退）并导出契约（env.json 58 键）；宿主新增 `MCPConfig` 字段与 `memoryEnvironment()`，两个注入点同时生效
- 2026-11-02 公开发布改造：`README.md` 重写为完整英文对外文档，新增 `README.zh.md` 中文镜像（章节一一对应）；新增 MIT `LICENSE`；`pyproject.toml` 补 `readme` / `license` / `license-files` / `keywords` / `classifiers` / `urls` 并把 hatchling 提到 `>=1.27`（PEP 639）；profile 改 public / MIT。验证：477 测试通过、ruff 通过、wheel 元数据含 `License-Expression: MIT`、资源已打包
- 2026-11-02 project_notes 可靠性：查询路径永不嵌入（`NOTE_QUERY_EMBED_BUDGET=0`），缓存锁改按项目、忙时只读打开并调度后台预热；扫描排除 `tests/eval` 与 `.pytest_cache`；嵌入按 8 文件批次提交并逐批记日志；handler 加 20s 硬超时返回 `status=partial`+`timeout`；`_enforce_embedder_identity` 在未设 `EMBEDDER_MODEL` 时拒绝清库。实测 gateway 缓存 3663→603 chunks（清掉 3058 条 eval 脏数据），查询 66–464ms。全量 484 通过（1 例 token_proxy 基线失败）
- 2026-11-03 审查整改（#6-#14）：`gateway_log` 新增 `upstream` 列，代理的 4xx/5xx 与 502 写入 `error_type`/`error_message`/`upstream`，>60s 另打 `gateway_slow_request`；`api_usage` 写入 `route_tier`（provider）与 `route_reason`；同协议非流式响应开始记用量；`_marginalia_retry` 超时下限 1s 并补回归测试；`_cascade_parallel` 注明超时只延迟回收；`known_item_eval` 改用 `gateway_home()`；修复 tool_output 去重测试的固定日期。全量 502 通过
- 2026-11-03 基座升级 1.0.0→1.4.0：补 `.agent/backlog.md`、`upgrades.md`、`plans/_template.md`；画像补 `stack.lockfile: none`、`delivery.trunk_branch: master`、`verify.outdated`；规则合并 CTX-04~06、VCS-07 并新增 observability；技能更新 build/review/init/learn/hunt/upgrade，新增 delegate/operate；启动契约与规则索引同步；跳过 ship/deploy/ui（画像不适用）。`verify-instantiation` 0 errors / 1 warning（lockfile）
- 2026-11-03 部署复验：代理与 MCP 19:59 重启，安装副本 7 个改动模块均与工作区一致；`gateway_log` 已有 `upstream` 列；重启后 8 条 `api_usage` 全部写入路由字段（deepseek/model_match、mimo/default_provider）。重启后尚无 4xx/5xx 样本，502 字段待下一条错误复验
- 2026-11-03 路由详情打点：`api_usage.route_detail` 记录 `requested=<原 model>`；未命中任何 provider 时额外打 `route_fallback` 警告；新增 2 例测试，全量 503 通过。20:24 已部署，摘要请求确认为 `claude-sonnet-5`
- 2026-11-03 按主题提交 8 个 commit（607f80c..d9bef06），工作区干净；未推送
- 2026-11-04 解耦配置与路径：新增 `core/env_compat.py`、`core/config_compat.py`、`core/project_paths.py`；默认配置改为 `agent-gateway.yaml`（原生 schema），旧 `config.yaml` 只读翻译；`GATEWAY_*` 统一命名，`UTILITY_*` 别名保留一个周期；`daemon.state_dir()` 与 `env_builder.config_root()` 改走 `gateway_home()`
- 2026-11-04 项目数据目录迁移：`graph_tools` / `search_notes_cache` 统一经 `project_paths` 解析 `.agent-gateway/`，旧 `.utility/` 只复制回退不删；`GATEWAY_PROJECT_DIR` 可覆盖；新增 `test_project_paths.py`
- 2026-11-04 图库写权移交：新增 `services/graph/store.py`（唯一 schema 所有者）与 `project_scan.py`（walk/对账/全量重建）；协议增 `sync` / `status`，新增 `agent-gateway graph index|status`；`contracts/graph.json` 导出引擎与 schema 版本
- 2026-11-04 工具输出去重更名：`skillDedupEnabled` / `MODEL_PROXY_SKILL_DEDUP` / `proxy_skill_dedup.py` / 注记键统一改为 `toolOutputDedup` / `MODEL_PROXY_TOOL_OUTPUT_DEDUP` / `proxy_tool_output_dedup.py`；旧键与旧 env 继续识别
- 2026-11-04 验证：全量 522 例通过，`ruff check` 通过；`GATEWAY_HOME=$(mktemp -d)` 下 `--check` 通过；`graph index --root` 生成 `.agent-gateway/graph.sqlite`
- 2026-11-04 隔离端到端：`HOME` + `CFFIXED_USER_HOME` 指向临时目录，venv 软链 gateway `.venv`；`utility -a "sleep 18"` 真实启动，首建 6 节点/2 边，运行中改文件触发 `index_file` 后增至 6 符号，`project_lookup`/`project_impact` 命中；隔离配置自动生成，项目根未出现 `.utility`
- 2026-11-04 部署验证修复：1.1.45 首启装 wheel、MCP/代理健康、配置生成均正常；全量 `sync` 因压缩 JS `marked.umd.js` 产出重复 `sym_key` 触发 `UNIQUE constraint failed: symbols.sym_key`。`store.replace_graph` / `replace_file` 增加按 key 去重，新增回归测试；全量 523 例通过，修复后源码重建得到 4546 符号/2861 边。需重新打包部署
- 2026-11-04 1.1.46 部署复验：安装 wheel 含 `unique_symbols`；含 `marked.umd.js` 的临时项目全量 sync 成功（248 符号/47 边/24 ms）；MCP 9001、代理 8613 健康，`project_lookup`/`project_impact` 正常。旧 agent 会话 PID 4760/4766 仍运行，需退出后才能确认新会话
- 2026-11-04 审查修复：宿主生成配置保留 `search` 段；`copy_legacy` 改用 SQLite backup 覆盖 WAL；索引器复用 `analysis.ProjectIndex` 并补 `build` 跳过；`idleUnloadSeconds` 接入 env；legacy 嵌套 knowledge 可识别；`GATEWAY_CONFIG_FORMAT` 与 `EMBEDDER_IDLE_UNLOAD_SECONDS` 进契约；修正测试旧属性。全量 526 例通过
- 2026-11-04 1.1.47 部署复验：wheel 含统一 ProjectIndex 与 SQLite backup；生成配置含 `search`（parallel=true）与 idle=600；app 契约含两个新 env；含 `build/` 的临时项目全量 sync 只索引 2 文件、`build_symbols=0`；MCP/代理健康
- 2026-11-04 项目图库异步刷新：新增 `services/graph/indexer_marker.py`（索引器 PID/启动时间登记）与 `refresh.py`（每项目 30 秒后台重建、`refresh.lock` 防跨进程并发）；`project_lookup` / `project_impact` 接入；索引器 `main()` 接收项目根并注销；新增 18 例测试，全量 544 例通过；隔离 e2e 断言登记 live/stopped
- 2026-11-04 1.1.48 部署验证：安装 wheel 含 `indexer_marker.py` / `refresh.py`；MCP 直接查询验证缺失图库和旧图库都在约 30 秒后重建，第二次查询命中；部署版索引器重复 SIGTERM 会中断注销，留下死条目（下一次查询会清理），源码已在 `finally` 中忽略后续 SIGTERM
- 2026-11-04 1.1.49 部署复验：安装包与随包 wheel 均含 `SIG_IGN` 修复；真实 `utility -a` 索引器登记 live，退出后直接为空；MCP `project_lookup` 缺失图库 35 秒后重建并命中；旧 agent 会话 30624/30631 仍运行 1.1.48 索引器，重启会话后才会登记
- 2026-11-04 15:40 重启复验：旧 30624/30631 已退出；新会话 41753 + 索引器 41760（命令带项目根）；utility 项目 `indexer.json` 登记 live；`project_lookup` 后未生成 `refresh.lock`，活索引器跳过刷新生效
- 2026-11-04 审查修复：`ps` 固定 `TZ=UTC`，避免索引器与 MCP 时区不同误判 PID 复用；`os.kill` 捕获 `OverflowError`；`refresh` / `project_scan` 改为按需导入，避免 MCP 常驻 tree-sitter 解析器；新增 handler 接线测试。全量 549 例通过，e2e 通过
- 2026-11-04 1.1.50 部署复验：安装包与随包 wheel 含 TZ / Overflow / 延迟导入修复；真实 `utility -a` 索引器登记为 UTC 时间、退出后为空；MCP 缺失图库 35 秒后重建并命中；旧 agent 会话 41753/41760 仍是 1.1.49 索引器，登记时间为本地时区，需重启会话
- 2026-11-08 记忆维度落地：写入侧 `remember`/`remember_batch` 接受 project/subsystem/task/task_type/ticket/language/interaction/session_id/role（枚举非法值置空、language 缺省按内容检测、批次顶层可注入）；recall 新增同名字段，project/ticket/language 硬过滤，subsystem/task/task_type/role/session_id 仅重排不改分；通用检索层新增 `Query.filters`/`Query.boost`（`pipeline._narrow`/`_prefer`），删除 MCP handler 内的 project+task 聚类加权；591 例通过
- 2026-11-08 维度部署复验（三轮，通过）：查询侧 project 过滤返回行全部匹配（`filtered=19/34`）、历史行补 language、无过滤 `filtered=0`；写入侧 task_type/language/narrative_role 均正确回显，boost `role` 生效。过程中发现并修复 mem0 两处冲突：promoted 键 `role` 被提升到行顶层、随后被消息角色覆盖为 `user`，最终存储键改为 `narrative_role`（工具参数名仍为 role）；601 例通过
- 2026-11-08 维度改动审查修复：硬过滤提前到融合截断前并放大候选窗口；冷层摘要继承源记忆维度、扩展结果与历史行按需补 language；dedupe 合并缺失维度；维度校验放宽为 strip+128 字符上限（允许中文与 `#`）；`Query` 增加 `expose` 并在重建/语义变体分支完整透传 filters/boost；`_prefer` 复用 fusion.rank_key；recall 返回 `filtered` 与被过滤提示；维度定义迁至 `services/memory/dimensions.py`；598 例通过
- 2026-11-08 本地小模型迭代检索实验：Qwen3-1.7B Q8_0（llama.cpp Metal）热态判定 p50 492 ms、解析率 99.27%；20 条 query 迭代新增 34 条强关联，人工认可 20 条（宽泛意图 15/15，专有验证意图 5/15）；判定器基线 precision 约 85%；结论不接入默认路径；episode 扩展对照（20 条 query、11 条有效扩展）precision 63.6%，与 LLM 迭代相当且零模型延迟，下一步优先修 episode 分组边界。脚本 `scripts/iterative_recall_eval.py`，报告 `tests/eval/iterative/2026-11-08/{report,episode_report}.md`；新增 `remember_batch` 顶层 `episode_id`（同批未指定条目共享分组），578 例通过，待重启 MCP 生效
- 2026-11-08 记忆分组治理 MVP：新增 `services/memory/group_governance.py`（多成员组 + 单条组近邻候选、批量 keep/split/merge 解析）、中英提示词、`scripts/group_governance_eval.py` 与 12 例测试；生产库只读报告 454 组 + 20 对；LLM 小样本 10 决策、0 解析失败；计划 `.agent/plans/2026-11-08-group-governance.md`，报告在 `tests/eval/group_governance/2026-11-08/`（未入库）

- 2026-11-08 分组治理审查修复：只读 `mode=ro&immutable=1` + 分页加载（生产 5322 条不再漏 322）、单条显式组不进配对、singles 带 `text_source`/`truncated`、批次 32k 字符预算、LLM 异常与畸形 split 隔离、报告标注明细截断；新增 `store.readonly_connect`、`link.fact_from_data` 与 8 例测试；624 例通过

- 2026-11-09 分组治理接入日任务：`govern_daily` 按 `since` 后新记忆所在组筛选候选、默认应用 split/merge、写 `episode_prev`/`episode_source`/`governance_run`、日任务预算 12 次调用；应用时同步 mem0 metadata 并重建 episode 边；回滚脚本先同步 mem0、只扫 hot；调度器传上次日任务时间；629 例通过

- 2026-11-10 记忆神经网 v2（T1-T18）：新增 `services/memory/{network,network_build,strength,lifecycle}.py`、`retrieval/arms/associative.py`、`prompts/{zh-Hans,en}/neuron_link.md`、迁移/基准/E2E/回滚四个脚本；`memory_graph` 增加 `synapse`/`associative` 边，`recall` 接入关联扩展，`/health` 暴露网络统计；18 项任务全部落地。审查发现并修复：reconcile 以陈旧 `member_count` 删活神经元（改查成员表）、`unbind_weak` 立即归档新建共激活突触（改按创建时间）、扩散首达锁定丢弃更强路径、关联结果未排除已命中 id、purge 在热/冷存储缺失时仍删图行、weekly dry-run 仍写分层/突触/度数、group_pairs 候选被 `no_candidates` 短路、治理写失败未回滚、`merge_neurons` 只迁 200 成员、回滚导出 BLOB 失败且 drop 会被重建、`ACTIVATION_FLOOR=0` 被吞、archive/trash 配置未消费。验证：710 例通过、ruff 通过、隔离 E2E 全项 PASS（生产库哈希不变）、50k/5k/100k bench spread p95 72ms；另在 Lume macOS 26.4 一次性 VM 内复跑 710 例、真实数据 E2E、100k/10k/200k 基准与 100 次批量召回，全部通过。期间修复 E2E 脚本 `_config_qdrant` 只认 JSON 导致 YAML 配置下误把仓库目录当 Qdrant 源的问题。

## 下一步

1. 推送已提交的 8 个 commit（需用户授权）
2. 打包部署记忆体第二轮改进并复验（近邻候选耗时、dry-run 不落盘、归档过滤后的召回）
3. utility 侧打包与整机回归通过后发版
4. 归档条目物理迁出热层：未开始，细节与触发条件见 utility 仓 `.claude/plans/memory-system.md` 的「待办」章节
5. 分组治理检查点 B：人工抽检 50 组；准确率 ≥85% 且扩展净增益 >0 后再做完整版任务 5-7

## 最近验证

| 时间 | 命令 | 结果 |
| --- | --- | --- |
| 2026-10-26 | `ruff format --check src tests scripts` | 通过（153 文件） |
| 2026-10-26 | `ruff check src tests scripts` | 通过（0 条） |
| 2026-10-26 | `.venv/bin/python -m unittest discover -s tests -t .` | 通过（128，1 skipped） |
| 2026-10-28 | `.venv/bin/python -m unittest discover -s tests -t .` | 通过（145，1 skipped） |
| 2026-10-28 | `ruff format --check` / `ruff check` | 通过（158 文件 / 0 条） |
| 2026-10-28 | `scripts/backfill_episode.py` 副本实测 | 写回 4199 条 / 3421 组 / 回滚清空 4199 条 |
| 2026-10-28 | 线上回填与召回复验 | 回填 4205 条 / 3426 组；RSS query 返回 3 条 expanded，同组 ep_20260906_1226 |
| 2026-10-28 | `package-utility.sh` | Utility-1.0.153 打包成功，bundle wheel 含全部修复 |
| 2026-10-28 | `.venv/bin/python -m unittest discover -s tests -t .` | 通过（150，1 skipped） |
| 2026-10-28 | 删除 rss 后 `ruff format --check` / `ruff check` / `unittest` | 通过（153 文件 / 0 条 / 160 例，1 skipped） |
| 2026-10-28 | `UTILITY_DATA_DIR=~/.config/utility agent-gateway --check` | 通过 |
| 2026-10-28 | `agent-gateway --export-contracts` | entries 5 项、env 无 RSS_DAILY_CONFIG |
| 2026-10-28 | 真实 8 页 trafilatura 提取（临时 venv） | 8/8 成功，链接占比全 0%，1 页尾部残留推荐 |
| 2026-10-26 | `.venv/bin/python -m build` | 通过（wheel + sdist） |
| 2026-10-26 | `UTILITY_DATA_DIR=<临时根> agent-gateway --check` | 通过 |
| 2026-10-28 | `ruff format` / `ruff check` / `unittest discover` | 通过（172 例，1 skipped） |
| 2026-10-28 | 离线阈值实验（副本 mem0，4260 条） | 0.65/0.70/0.75/0.80/0.85 单条组占比 64.5%/71.1%/79.8%/88.0%/94.5% |
| 2026-10-29 | `ruff format --check` / `ruff check` / `unittest discover` | 通过（155 文件 / 0 条 / 177 例，1 skipped） |
| 2026-10-29 | `ruff format --check` / `ruff check` / `unittest discover` / `agent-gateway --check` | 通过（155 文件 / 0 条 / 188 例 1 skipped / check passed） |
| 2026-10-29 | 工具面收敛后 `ruff format --check` / `ruff check` / `unittest discover` | 通过（154 文件 / 0 条 / 186 例，1 skipped） |
| 2026-10-30 | 检索质量修复后 `ruff format --check` / `ruff check` / `unittest discover` / `agent-gateway --check` | 通过（157 文件 / 0 条 / 213 例 1 skipped / check passed） |
| 2026-10-30 | recall 精度修复后 `ruff format --check` / `ruff check` / `unittest discover` / `agent-gateway --check` | 通过（158 文件 / 0 条 / 223 例 1 skipped / check passed） |
| 2026-10-30 | 部署后 recall 批量（100 条部署前历史去重 + 20 正例 + 20 负例 + 1 条复测） | 141/141 成功；历史 82 ok/12 weak/6 no_match；正例 11 ok/9 weak；负例 14 no_match/6 weak/0 ok；旧低分 36→0；旧 expanded 47→related 49；p95 1350ms |
| 2026-10-30 | 线上 MCP 14 项目批量（86 次调用） | 86/86 通过 |
| 2026-10-30 | 线上 MCP 负载（140 structure + 42 负例 + 25 catch/read） | 207/207 通过；p95 structure 58.8ms、负例 1623.7ms |
| 2026-10-30 | 检索层重构后 `ruff format --check` / `ruff check` / `unittest discover` / `agent-gateway --check` | 通过（179 文件 / 0 条 / 244 例 1 skipped / check passed） |
| 2026-10-30 | 固定 Qdrant 快照下新旧实现 recall 对比（100 条） | 完全一致 100/100（status + ids + related） |
| 2026-10-30 | 开关组合评测（固定快照，正例 20 + 负例 20） | base/split/relative 负例 ok 均为 0；term_split 曾把负例 weak 从 6 提到 14，改为词法臂用原句后归零 |
| 2026-10-30 | 标注集导出（368 条 recall query，分层按语言） | 0 错误、平均 245ms；每 query 5 条结果，定位带短 id 保证唯一 |
| 2026-10-30 | 基线评测（368 条标注 → 249 条 gold → 244 条 specific） | Recall@5 1.0 / MRR@10 0.887 / nDCG@10 0.878 / precision@5 0.421 / top1 相关率 0.393 / 平均相关 0.88 条 |
| 2026-11-02 | `ruff format --check` / `ruff check` / `.venv/bin/python -m unittest discover -s tests -t .` | 通过（227 文件 / 0 条 / 477 例） |
| 2026-11-02 | `.venv/bin/python -m build` | 通过；wheel 元数据 `License-Expression: MIT`，LICENSE、提示词、语言包与 ast-js 已打包 |
| 2026-11-03 | `ruff format --check` / `ruff check` / `.venv/bin/python -m unittest discover -s tests -t .` | 通过（230 文件 / 0 条 / 502 例） |
| 2026-11-03 | 新增测试重复 5 轮（embedder_lifecycle + search_runtime） | 5/5 通过 |
| 2026-11-03 | 线上 `tools/list` 直连 9001 | 28 个工具，无 `project_search`/`project_structure` |
| 2026-11-03 | `agent-vibe/scripts/verify-instantiation.sh` | 0 errors / 1 warning（`stack.lockfile: none`） |
| 2026-11-03 | 基座升级后 `ruff format --check` / `ruff check` / unittest | 通过（230 文件 / 0 条 / 502 例） |
| 2026-11-03 | 部署比对（工作区 vs 安装副本） | 7 个改动模块全部 same；`/health` 正常 |
| 2026-11-03 | 重启后 `api_usage` 路由字段 | 8/8 有值；deepseek/model_match、mimo/default_provider |
| 2026-11-03 | 路由详情打点后 `unittest discover` | 通过（503 例） |
| 2026-11-04 | `ruff format --check` / `ruff check` / `unittest discover` | 通过（549 例） |
| 2026-11-04 | `bash scripts/e2e-isolated.sh`（utility） | 通过；登记 live → 停止后空 |
| 2026-11-04 | 审查修复后 `unittest discover` / e2e | 通过（549 例 / 隔离 e2e） |
| 2026-11-04 | 1.1.48 MCP `project_lookup` 缺失图库 | 第一次 error；35 秒后重查 ok（L3） |
| 2026-11-04 | 1.1.48 MCP `project_lookup` 旧图库 | 第一次 no_match；35 秒后重查 ok，命中 `added`（L3） |
| 2026-11-04 | 1.1.48 部署版索引器退出 | 登记 live；退出后条目残留，下一次查询清理（已在 1.1.49 修复） |
| 2026-11-04 | 1.1.49 部署版索引器退出 | 登记 live；退出后条目为空（L3） |
| 2026-11-04 | 1.1.49 MCP `project_lookup` 缺失图库 | 第一次 error；35 秒后重查 ok（L3） |
| 2026-11-04 | 15:40 新 agent 会话 | 41753 + 索引器 41760；登记 live；查询后无 `refresh.lock` |
| 2026-11-04 | 1.1.50 部署版索引器退出 | 登记时间为 UTC；退出后为空（L3） |
| 2026-11-04 | 1.1.50 MCP 缺失图库刷新 | 第一次 error；35 秒后重查 ok（L3） |
| 2026-11-08 | `ruff format --check` / `ruff check` / `.venv/bin/python -m unittest discover -s tests -t .` | 通过（260 文件 / 0 条 / 600 例） |
| 2026-11-08 | 部署环境 MCP `recall` 实测（project 过滤/对照） | 过滤行全部带 project、`filtered=19`；对照 `filtered=0`；写入 task_type/language 生效 |
| 2026-11-08 | 第二轮部署复验（role 维度） | 回显值为 mem0 消息角色 `user`，证实键名冲突；改存储键 `narrative_role`，601 例通过 |
| 2026-11-08 | 第三轮部署复验 | 新写入行回显 `narrative_role=verification`；project 过滤 `filtered=34`；boost role 生效 |
| 2026-11-08 | 分组治理 MVP：`unittest discover` / `ruff format --check` / `ruff check` / `GATEWAY_HOME` 临时目录 `--check` | 613 例通过 / 265 文件 / 0 条 / check passed |
| 2026-11-08 | `scripts/group_governance_eval.py` 生产库只读 + 1 次 LLM | 454 组 + 20 对；10 决策；0 解析失败 |
| 2026-11-08 | 审查修复后全量测试与只读连接 | 624 例通过；ruff 266 文件通过；只读读生产库 2 行成功 |
| 2026-11-10 | 记忆神经网 v2 T1：`PYTHONPATH=src .venv/bin/python -m unittest tests.test_memory_network` | 13 例通过；全量 666 例通过；ruff format/check 通过 |
| 2026-11-10 | 记忆神经网 v2 审查修复后 `unittest discover` / `ruff check` / `swift build` | 710 例通过 / 0 条 / Build complete |
| 2026-11-10 | `scripts/memory_network_e2e.py --copy-prod --port 9013` | all_ok：迁移 5540 节点幂等、a→b→c 扩散、MCP associative、清理 2 条、治理合并 1 对、recall 返回、生产 graph/qdrant 哈希不变 |
| 2026-11-10 | `scripts/memory_network_bench.py --nodes 50000 --neurons 5000 --synapses 100000` | spread p95 72ms、decay 1.44ms、tiers 1390ms，预算全通过；degree p95 20 |
| 2026-11-10 | Lume VM（macOS 26.4）全量 `unittest discover` / `ruff check` | 710 例通过（3 skipped）/ 0 条 |
| 2026-11-10 | Lume VM 内 `memory_network_e2e.py --copy-prod`（真实 5556 节点 + 6106 向量） | all_ok：迁移幂等、a→b→c 扩散、MCP associative、清理 2 条、治理合并 1 对；生产 graph/qdrant 哈希不变 |
| 2026-11-10 | Lume VM 内 bench 100k 节点 / 10k 神经元 / 200k 突触 | spread p95 163ms、decay 2.16ms、tiers 3800ms，预算全通过；degree p95 20 |
| 2026-11-10 | Lume VM 内 100 次真实 recall + 20 次 associative 图查询 | 100/100 无错误（20 ok / 80 no_match，未复制 notes 层）；关联图 20/20 命中；recall p50 7.3ms、p95 13ms |
| 2026-11-10 | VM 拆除 | `lume ls` 为空；无 lume LaunchAgent；VM 内证据已回收到 `tests/eval/memory_network/2026-11-10/vm/` |
| 2026-11-11 | 重建 Lume 基础镜像 + 端侧克隆（用户要求镜像保留） | `macos-base-clean`（源 `macos-tahoe-vanilla`）保留；克隆 `utility-env` 复制 Clash/LLM 配置/记忆库/Utility.app |
| 2026-11-11 | `utility-env` 内完整流程实跑（真实代理 + 真实 LLM + 5587 节点） | daily 200/500s（pairs 11、compressed 8、governance applied 1）；weekly dry-run 无写；recall 50/50 ok（p50 668ms）；graph 20/20；证据 `vm-fullflow/` |
| 2026-11-11 | VM 内神经网批量实验：3k 节点/600 神经元、20k 节点/4000 神经元 | 迁移/关联(910、2000)/扩散/强化/衰减/解绑(40)/分层/度上限/合并/拆分全部按预期；20k 全流程 113.5s；证据 `tests/eval/memory_network/2026-11-11/` |
| 2026-11-11 | 移除后台 LLM 输入截断（关联审查 400 字、治理正文 4000 字） | 改为完整正文；新增完整正文用例；712 例 + ruff 通过；VM 内 6000 字正文实测完整 |
| 2026-11-11 | 真实库 16 轮（112 天）生命周期演化 | active 16 / warm 0 / archive 5580，神经元 4217/成员 5596 全程不变、0 orphan；证据 `stability-16w.json` |
| 2026-11-11 | 真实 LLM daily 三轮 + 16 轮稳定态 | 每轮 100 对审查、写入 86-87、耗时 13-19 分钟；突触 4→98（权重 0.69-0.94）；112 天后 active16/archive5593、0 orphan；证据 `real-llm.md` |
| 2026-11-11 | 关联审查并发 + 全量回填 | 新增 `MEMORY_NEURON_REVIEW_CONCURRENCY`（默认 1）接入 Python 与宿主；2000 对并发 10 回填 25.6 分钟、写入 1096 条，突触 98→1108、度 max 7；714 例 + ruff + swift build 通过；证据 `backfill.md` |
| 2026-11-11 | 检索质量评测（15 自由查询 + 7 指定关键词） | 指定词 7/7 命中主题、无关 0；发现长句整句作为 keywords 会假阴性（0→拆词后 8）；覆盖受 limit 限制；证据 `recall.md` |
| 2026-11-11 | 真实 Claude Agent 检索评测（VM 内 8 条意图） | 7/8 回答与库中记忆吻合；绣仪因 literal_coverage=0.5 被多词/中英混写误杀（单词语 ok 6、加 Xiu Yi 后 no_match）；agent 会自拟词并多轮换词；证据 `agent-recall.md` |

| 2026-11-11 | Lume VM 内 v2 修复复验（真实 LLM daily/weekly + 心跳 + recall） | daily 233.6s / weekly 242.3s；期间 health max 204/326 ms、0 错误；written 19、`llm_skipped=false`；coactivation 13；724 例 + ruff 通过 |

## 已知问题

- 2026-11-10 关联检索臂默认开启与 `retrieval-no-fitted-thresholds` 规则冲突：该规则要求新排序信号在没有同一评测集前后对比时默认关闭，并要求首位命中率/上下文长度数据。当前 `MEMORY_NEURON_ENABLED` 默认 true（用户既定「降级默认 ON」与最完善实现的延续），尚无 associative 臂的 before/after 指标；已在 `.agent/decisions.md` 记录为待评测风险，下一步用固定快照做开关对照后再定默认值。
- 2026-11-10 网络层阈值（`PAIR_FLOOR=0.56`、`MIN_SYNAPSE_WEIGHT=0.62`、半衰期 30/90 天、分层 0.45/0.20、idle 30/90 天）仍为代码常量，未走 `floors_for` 或用户配置；属上述规则的同一待标定项。

- 2026-11-08 自动化截断审计与修复：8 处判断输入改为完整正文（分组取 `fact`、矛盾 500、合并 150/120/80、recall 语义过滤 200 全部移除截断）；`link.py` 白名单补 `fact` 并从 Qdrant 回填 5610 条（99.9%，备份 `memory_graph.sqlite.bak-20261108225909`）；616 例通过。待重启 MCP 使判定与白名单修复生效后复验（`todo-20261108-002`）。
- 2026-11-08 分组治理审查修复：5 类缺陷已修（只读分页、显式单条组、输入标记、批次预算、输出校验）；回填脚本改为读分片副本并剥离 mem0 `Context:` 后缀，已 apply 1046 条（备份 `memory_graph.sqlite.bak-20261108235634`），fact 覆盖 5332/5332、候选 654→674。`SUMMARY_FACTS_MAX_CHARS=20000` 与 `SUMMARY_RULE_SUMMARY_MAX_CHARS=2000` 取代无界输入。待重启 MCP 复验。

- `test_ast_imports` 中调用主项目二进制的用例被跳过：本仓没有 `utility` 可执行文件，属预期降级。
- `pip-audit` 尚未安装，交付门暂未执行。
- 冷层 774 条（created_at ≤ 2026-10-22）缺 `source_memory_ids`：热层 processed 在 10-23 批量迁移后丢失消费日期，历史来源不可恢复，决定不回填（见 decisions 2026-10-28）。消费方 `_mark_cold_stale` 对缺键与空列表行为一致，无功能影响。
- Claude Code 侧 `Invalid request parameters`/`-32602` 是 MCP 服务进程替换后的客户端连接瞬态；重连逻辑在外部客户端，不在本仓，服务端 `/health` 已存在。
- `project_search` 向量层绝对阈值仍为 0.35；中文向量-only 已直接 `no_match`，英文无词法锚点仍可能返回 `weak_match`，需人工复核。
- `recall` 的 0.68/0.56/60% 阈值来自历史分数分布、批量正负例与单测；不同 embedding 版本可能需要重新标定。
- `recall` 的正例 `weak_match` 多为 notes-only；调用方应按 `status` 判断，不应把 notes 当语义记忆命中。
- 2026-11-01 记忆折叠审查修复（11 项）：`level`/`importance`/`protected` 进 `node_meta` 白名单并在去重分支传播；折叠排除已命中摘要、跳过 protected、按 bucket 分组、cap=1 全折叠、只归档已读到正文的节点；`set_memory_status` 增加 `db_path`；热臂图库回填失败不再丢弃结果；`_touch_hot_access` 移到截断之后并走 to_thread；评测脚本默认走 `GATEWAY_HOME` 且校验样本与连通性；新增不 mock `_write_memory` 的折叠集成测试；全量 387 例通过
- 2026-11-01 部署验证与两处修正：`memory_protect_importance()` 在配置为 0 时返回 None（原先返回 0 让每条记忆都被判为受保护，周任务矛盾检测静默归零）；`tests/__init__.py` 把 `GATEWAY_CACHE` 指向临时目录，测试不再写进生产的 `~/.cache/utility/server.log`，`test_mcp_environment_defaults` 改为断言 `paths.gateway_cache()` 派生；按 CI 命令 `discover -s tests -t .` 全量 391 例通过
- 2026-11-03 `cache_write_tokens` 仍恒 0：当前 deepseek/mimo 的 anthropic 端点不返回 `cache_creation_input_tokens`；resolver 已支持该字段，属上游限制而非采集缺失
- 2026-11-03 live MCP 曾出现 `project_search`/`project_structure`：直连 9001 `tools/list` 已无这两项，来源是客户端旧会话缓存，重启客户端即消失
- 2026-11-03 `~/.config/agent-gateway` 残留与安装副本 `path_filter.py`/`router.py` 已确认无引用；已移入废纸篓
- 2026-11-03 基座校验保留 1 条 warning：`stack.lockfile: none`。项目按 pyproject extras 安装，暂无锁定文件；策略见 `.agent/backlog.md` todo-20261103-005
- 2026-11-03 依赖升级扫描已写入 `.agent/upgrades.md`；本轮未升级任何依赖，批次待用户确认
- 2026-11-03 路由详情已上线：`<transcript>` 后台摘要请求实际 `requested=claude-sonnet-5`（20:24 后 12/17 条），回退到 `mimo-v2.6-flash`；用户判定这是 Claude 客户端请求，无需处理（todo-20261103-007 dropped）。主循环 deepseek-flash 均为 `model_match`
- 2026-11-03 重启后尚无 4xx/5xx 请求，`gateway_log` 的 `error_type`/`upstream` 线上样本待补；历史 502 行仍为空属预期
- 2026-11-08 基座升级 1.4.0 → 1.5.0：新增 `steward`/`sandbox` 技能、`.agent/agent-health.md` 基线、`.agent/sandbox.md`、`.agent/.maintenance-state`、维护钩子与 SessionStart 注册；profile 补 `maintenance_*` 与 `isolation_*`。`verify-instantiation.sh` 0 errors / 1 warning（既有 `stack.lockfile: none`）。首次 `steward` 体检未执行
- 2026-11-08 `pip-audit 2.10.1` 装入 `.venv` 并加入 `dev` extras；全环境审计 0 已知漏洞，本地包 `agent-gateway` 不在 PyPI 故跳过
