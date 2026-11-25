# 决策日志

| 日期 | 级别 | 决定 | 决定者 | 依据 | 可回退方式 |
| --- | --- | --- | --- | --- | --- |
| 2026-10-26 | D1 | 三仓边界：agent-gateway（能力层）/ utility（macOS 宿主）/ workspace（Node 服务端 + RN/Expo） | 用户 | 职责分离，禁止宿主内耦合 | 合并回单仓需重做契约与打包 |
| 2026-10-26 | D1 | 服务端用 Node/TS，与 RN 同栈；gateway 只提供 MCP 与 LLM | 用户 | 与 RN 生态一致 | 服务端改回 Python 需重写协议层 |
| 2026-10-26 | D1 | 宿主打包时携带 gateway wheel，首次启动装入固定 venv | 用户 | 离线可用，保持自包含 | 改私有 PyPI 或源码拷贝 |
| 2026-10-26 | D1 | workspace 使用 npm workspaces，不用 pnpm | 用户 | 用户指定 | 切换需重写脚本与锁文件 |
| 2026-10-26 | D3 | 采用 src layout（`src/agent_gateway/`） | Agent | PyPA 指南：避免 cwd 源码副本掩盖打包遗漏 | 移回根目录并改 pyproject |
| 2026-10-26 | D3 | 保留 unittest，不迁移 pytest | Agent | 现有 128 例；pytest 仅带来便利，非必需依赖 | 引入 pytest 后可直接跑现有用例 |
| 2026-10-26 | D3 | import 名独立为 `agent_gateway`，distribution 为 `agent-gateway` | Agent | 与宿主彻底解耦 | 改回 `utility` 会与宿主历史命名冲突 |
| 2026-10-26 | D3 | 服务端配置独立 `server.yaml`，不并入 `config.yaml` | Agent | 桌面端 `ConfigManager.save()` 会重写 config.yaml、丢弃未知段 | 合并需改宿主保存逻辑 |
| 2026-10-26 | D3 | 契约以 `contracts/*.json` 导出，跨仓读文件校验 | Agent | 跨仓后无法再同时扫描 Swift 与 Python 源码 | 恢复源码扫描需合并仓库 |
| 2026-10-26 | D3 | lint 暂为报告态，不阻塞 CI | Agent | 迁移代码有 642 条历史告警，基座阶段不做大规模清理 | 清理完成后移除 `continue-on-error` |
| 2026-10-26 | D3 | lint 基线清零并转为阻塞门：自动删除未使用导入；两个兼容再导出模块加 per-file-ignores；E402 由移动 logger 赋值解决；测试注入点保留 noqa | Agent | `ruff check` 0 条、128 测试通过；再导出模块 docstring 明示用途 | 回退本提交并恢复 `continue-on-error` |
| 2026-10-28 | D1 | i18n 两仓各自自持：gateway 用 `locales/<lang>.json` 运行时加载，宿主不再生成也不再校验 Python 语言包 | 用户 | 框架不依赖宿主；生成器跨仓耦合是 P2 的剩余问题 | 恢复 `gen-l10n.py --python-out` 与宿主 contract 的 `--python-root` |
| 2026-10-28 | D1 | RSS 日报业务编排归宿主（utility `RSSDigestService`），gateway 只保留 `/api/summarize`、`/api/read`、`/api/llm/chat` 通用能力 | 用户 | utility 是业务层、gateway 是框架；提示词与调度属业务 | 恢复 `services/rss` 与 `rssDaily` 入口（废纸篓有原文件） |
| 2026-10-28 | D1 | 冷层历史 774 条 `source_memory_ids` 不回填，登记为不可恢复限制 | 用户 | 全部 created_at ≤ 2026-10-22；热层 processed 在 10-23 批量迁移抹平消费日期，无法按日反推；伪造 id 会错误传播 stale | 若找到历史 consume 日志可补 |

| 2026-10-28 | D3 | 冷层摘要按来源热层记忆的多数派 episode 继承分组（显式优先，无来源则新建，查源上限 200） | Agent | 摘要由多条热层记忆汇总，单条 episode 无法表达；多数派与写入时的消费集合一致 | 清空冷层 `episode_id` 即可回退 |
| 2026-10-28 | D3 | `EPISODE_INHERIT_SIMILARITY` 保持 0.80，不下调 | Agent | 4260 条离线实验：0.75 仅把多成员组从 418 增至 590，单条组仍 79.8%，新增组相关性未验证；扩展宁可少而准 | 改常量后重跑 `scripts/backfill_episode.py` 可重新分组 |

| 2026-10-28 | D3 | 推翻冷层「多数派」继承：改按摘要向量最相似的源记忆继承 `episode_id`，无向量时退化多数派 | Agent | 实测 5 条跨主题摘要共享同一 episode，扩展带出无关条目；摘要是一对多汇总，多数派无法表达主题 | 恢复多数派实现即可回退 |
| 2026-10-28 | D3 | 冷层继承增加 0.45 相似度下限，低于则新建组 | Agent | 副本模拟 5 条样本：最高分 0.745/0.648/0.619/0.557/0.359，0.45 是自然分界；弱继承会误扩无关条目 | 改回无下限或调整常量 |
| 2026-10-29 | D3 | 规则注入改为确定性位置策略：第 1 条真实 user 注入 full 块，其余注入内置英文短提示；`fullEvery` 可周期刷新，`mode=full` 回退 | Agent | 代理每次基于客户端原始历史重算，位置规则使历史消息跨请求字节一致；压缩后新首条自动重建 full；185 例测试通过 | env `MODEL_PROXY_RULE_INJECTION={"mode":"full"}` |

| 2026-10-29 | D3 | 按 agent-vibe 1.0.0 升级基座：拆分 `core.md`、补 `memory/INDEX` 与双钩子/PR 模板，迁入缺失的 concern/type 规则 | Agent | 种子升级协议；实例化校验报告 `core.md` 174 行超 120 预算、缺钩子与索引 | 恢复 `core.md` 单文件，移除新增规则与钩子 |
| 2026-10-29 | D1 | 移除 MCP 工具 `project_dependencies`、`project_exports`、`summarize`；`summarize` 仅保留 REST `/api/summarize` | 用户 | 前两者低频且可被搜索/读取替代；`summarize` 模型可直答，宿主业务仍走 REST | 恢复工具 schema、handler 与对应模块（废纸篓有原文件） |
| 2026-10-29 | D3 | `project_notes` 改为混合重排（语义 0.65 + 关键词 0.30 + 路径 0.05 + MMR 去重），并提供扫描后预热入口 | Agent | 冷启动首查 58.5s；重复文档占满结果；预热在 project_search/structure 与 `/api/project/notes/prewarm` 触发 | 恢复旧排序与移除预热调用 |
| 2026-10-29 | D3 | `project_notes` 每次扫描清理不在当前扫描集合或项目根外的缓存行；连续短段落累积成块而非丢弃 | Agent | 项目迁移后旧绝对路径残留 133 文件/2728 分块（51%），规则清单类文档 0 分块 | 回退清理逻辑与旧分块器 |
| 2026-10-29 | D3 | `api_usage` 记录一次响应内全部工具（`tool_names` JSON），`tool_output_log` 按 `tool_call_id` 唯一，新增 `/usage/tools` 统计端点 | Agent | 原 `tool_name` 只记首个工具，非流式还回退到上一轮；近 5 天 1334/7127 次请求含多工具；同一调用最多重复 7 行 | 删除 `tool_names`/唯一索引与端点，恢复单工具统计 |
| 2026-10-30 | D3 | `catch_up` 默认返回 50 条、24000 字符，支持 `limit`/`offset`/`max_chars`；`read_url` 对 400/404/410 终止并增加 `failure_kind`；`project_structure` 增加 `status`；`project_search` 增加 `module`/`importedBy`/`importCount` | Agent | 30 天 1135 次调用：`catch_up` 91.7% 超长转存（22.3 万–146 万字符），`read_url` 17.6% 失败，`project_structure` 6.7% 空结构被当成功，`project_search` 20.8% 回退且只有 55.4% 结果被下一轮引用 | 恢复旧 handler；新字段为 additive，删除即可回退 |
| 2026-10-30 | D3 | `project_search` 仅有向量层结果时返回 `weak_match`，不再返回 `exact`；无词法锚点的候选被过滤并计入 `rejected`；中文向量-only 直接 `no_match`；旧 `symbols` 表缺 `start_line`/`end_line` 时回退到无行号查询 | Agent | 14 项目负例 10 个误命中；随机串分数 0.5036–0.5819，而相关 `ConversationFile` 仅 0.5308，单靠绝对阈值无法分离；词法锚点用标识符分词，单 token 查询整体匹配，中文直接 no_match；4 项目中文阈值扫描无可用分界，中文注释覆盖率 0.19%–3.87%，中文锚点 0/30 命中；`yuanyxh.github.io` 旧库报 `no such column: start_line` | 删除向量-only 分支、中文 no_match 与旧表回退，恢复原查询 |
| 2026-10-30 | D3 | `recall` 加相关性下限（hot 0.68 / cold 0.56 / expanded 0.56），episode 扩展分数改为 `min(seed*0.98, actual)`，notes 改为 ASCII 词 + 中文 bigram 覆盖率 ≥60%；主结果只含 direct，扩展放入 `related`，响应增加 `status` 与 `match_kind` | Agent | 随机串 query 仍返回 0.554–0.564 的不相关热层记忆；107 次历史 recall 中 19 次（17.8%）含 expanded，最多 6/10；扩展输出继承 seed×0.98，弱 sibling 获得强相关外观；notes 任一词命中导致旧 RSS 日报等宽泛命中；140 次部署后批量中 0.62/0.55 仍让 3 条负例返回 `ok`（hot 0.64–0.676、cold 0.550–0.555），离线重放 0.68/0.56 后负例 `ok` 为 0；最终部署复验 141/141 成功、负例 ok 0、旧低分 36→0 | 恢复 `threshold=0.0`、原扩展分数与 `results` 合并；`related`/`status` 为 additive，删除字段即可回退 |

| 2026-10-30 | D3 | 检索层收敛为协议 + 管线：`Retriever`/`Fusion`/`Gate`/`Reranker`/`Rewriter` 五个 Protocol，profile 由 `RETRIEVAL_REWRITERS`/`RETRIEVAL_GATE` 控制，新阶段默认关闭；阈值按 `model_tag` 分档，未标定即抛错；project_search/project_notes 先接 trace 不改算法 | Agent | recall 的阈值、分层与扩展散在 4 个模块，改一处无法证明整体变好；等价迁移先行可把『重构引入偏差』与『算法改进』分开验证 | `retrieval.enabled` 关闭即回旧路径；旧实现留在函数级包装 |

| 2026-10-30 | D3 | 检索层新阶段默认关闭；词法臂（notes）始终用原始查询，子查询只作用于 dense 臂；相对门控只对 semantic 层（hot/cold）生效，notes-only 只能 weak_match | Agent | 固定快照实测：term_split 让 6 条负例从 no_match 升为 weak_match（notes 覆盖率被短词放大），改为词法臂用原句后归零；relative 门控若把 notes 计入 top1/margin，20 条负例中 6 条被判 ok | 关闭开关即回旧行为；开关经 `retrieval.rewriters` / `retrieval.gate` 配置 |

| 2026-10-30 | D3 | 评测金标用「导出 Markdown 供人工判定」而非自动推断引用关系；定位列附加结果 id 前 8 位保证唯一；未判定行视为忽略，允许半份标注直接产出 gold | Agent | 「下一轮是否引用」受模型措辞影响不可靠；同一文件的多个分块 locator 相同，不加 id 会把两个分块合并成一条判定 | 删除 scripts 三个脚本即回退 |

| 2026-10-30 | D3 | notes 词法命中增加语义复核：命中片段重新 embedding 与 query 比余弦，低于 0.40 丢弃；只复核 coverage 前 6 条 | Agent | 标注 1496 条结果：lexical 层 742 条中仅 19 条相关（2.6%）；覆盖率分布与相关性完全重叠（不相关也有 coverage=1.0），提高覆盖率阈值无效；语义复核 0.40 可保留 70% 有用项、过滤 73% 噪声（覆盖率 0.8 只能保留 45%/过滤 61%）；12 条候选耗 450ms，6 条约 220ms | `RETRIEVAL_NOTES_SEMANTIC=0` 关闭 |

## 记录规则

- D1、D2 必须由用户决定，记录用户原话要点。
- D3 由 Agent 决定，记录默认值来源。
- 决定被推翻时新增一条，不删除旧记录。
| 2026-10-31 | D3 | 回退 progressive 规则注入：每条真实 user 消息重新注入完整规则块，删除 `modelProxy.ruleInjection` 与 `MODEL_PROXY_RULE_INJECTION` | Agent | 用户实测对「首条 full + 其余短提醒」的遵从率下降；配置面只增加行为漂移 | 需恢复时按 97d6240 复原 |

| 2026-11-01 | D3 | 周任务矛盾候选改为每节点 Qdrant 近邻（`query_batch_points` 分批 top-k，带内过滤去重），全量矩阵仅作失败回退 | Agent | 全量 O(n²) 在 3.6 万节点时约 5 GB 矩阵；forgetful 与 mcp-memory-service 的关联/建边均用采样或近邻，没有全量闭包 | 回退路径保留 `_band_pairs`，由近邻异常触发 |
| 2026-11-01 | D3 | 新增 `importance`(1–10)/`protected`；矛盾与归档跳过 protected 与达到 `MEMORY_PROTECT_IMPORTANCE` 的条目；周任务与 REST 增加 `dry_run` | Agent | 生产周任务曾直接 supersede 一条历史记忆；forgetful 有 protected 与默认 dry-run，我们缺这两个安全阀 | 阈值留空即只保护显式 protected；dry-run 不写边 |
| 2026-11-01 | D3 | `recall` 的 mem0 filters 加 `NOT archived=true`；新增 `SignalWeightedFusion`，recency/access/quality 权重默认 0 | Agent | 归档条目会占用 top_k 名额再被丢弃；多信号必须有同一评测集前后对比才允许开启，默认关闭不改变现有排序 | 清空权重即回纯分数排序；过滤失败回退结果后过滤 |
| 2026-11-01 | D3 | `MEMORY_PROTECT_IMPORTANCE`/`MEMORY_MAX_CONTENT_CHARS`/`RANK_SIGNAL_*` 先只读 env，未接入 utility 配置注入 | Agent | 避免在未产品化的阶段改动 Swift 配置契约；启用时须按宿主规则同时加 `baseEnvironment()` 与 `taskEnvironmentOverrides()` 两个注入点 | 不设 env 即全部关闭/无限制 |

| 2026-11-01 | D3 | 摘要按预算折叠：保留最新 `cap-1`，其余合并为一条 `level+1` 摘要（上限 3 层），旧摘要软归档并写 `derived_from`；预算默认 0 即不折叠 | Agent | 摘要无上界增长会持续占用热层；按相似度聚类折叠只会引入新的拟合阈值，预算触发是唯一可解释的条件 | `MEMORY_MAX_SUMMARIES` 置 0 即停用；层级上限由 `SUMMARY_LEVEL_MAX` 控制 |
| 2026-11-01 | D3 | 取代「`MEMORY_*`/`RANK_SIGNAL_*` 只读 env」一条：开关已接入宿主配置链（`MCPConfig` 字段 + `memoryEnvironment()` 两个注入点 + `_YAML_FALLBACKS`），并导出契约 | Agent | 用户要求同步配置；只读 env 的开关在宿主侧既不可见也无法启用 | 配置项保留默认 0，行为与未接入时一致 |
| 2026-11-02 | D1 | 公开发布：仓库公开，`.agent/` 与 `CLAUDE.md` 作为开发者文档保留公开；许可证 MIT；`README.md` 为英文权威入口，`README.zh.md` 为中文镜像 | 用户 | 用户明确选择公开开源、保留内部文档与 MIT；双语为发布要求 | 改回私有需撤销远端公开与许可证声明 |
| 2026-11-03 | D3 | `route_tier` 记录实际 provider 名，`route_reason` 记录 `model_match`/`default_provider`/`legacy_upstream`；不恢复旧 strong/weak 路由 | Agent | 当前仓只有 provider 选择，没有分层路由；旧列全空，按实际路由写入才能回答「这一轮走了哪个上游」 | 恢复留空即可；历史 strong/weak 行保留 |
| 2026-11-03 | D3 | 基座升级到 agent-vibe 1.4.0：合并规则/技能，补 backlog/upgrades/plans；跳过 ship/deploy/ui；lockfile 暂为 none | Agent | 用户确认升级；画像 `deploy_target: none`、`has_ui: false`；项目按 pyproject extras 安装，无锁定文件 | 按 `.agent/plans/archive/base-upgrade-1.4.0.md` 的清单与 git 历史回退 |
| 2026-11-04 | D3 | 项目内数据目录改为 `.agent-gateway/`，图库 schema 与写入归 gateway；utility 只发文件事件 | 用户 | utility 退化为客户端，宿主不应拥有图库写权；旧 `.utility/` 仅复制回退，避免迁移丢数据 | `GATEWAY_PROJECT_DIR=.utility` 回退旧布局；旧文件不移动不删除 |
| 2026-11-04 | D3 | 配置唯一写入格式 `agent-gateway.yaml`；旧 `config.yaml` 只读翻译一个发布周期 | 用户 | 配置 schema 与宿主解耦，同时保证存量用户无感升级 | `GATEWAY_CONFIG_FORMAT=legacy` 强制适配；移除适配需用户确认 |
| 2026-11-04 | D3 | `skillDedupEnabled` 更名 `toolOutputDedup`，env/模块/注记键同步 | 用户 | 实际判定是 byte-identical 的 `tool_result`，与 skill 无关，旧命名误导配置理解 | 旧配置键与 `MODEL_PROXY_SKILL_DEDUP` 保留一个周期 |
| 2026-11-04 | D2 | MCP SDK 升 2.3.0，服务端迁 `MCPServer`：14 个工具改为 `@mcp.tool()` 注解函数，`structured_output=False` 保持旧返回形状；2026-07-28 协议实测可用（SDK Client 探测到 2026-07-28，tools/list 与 tools/call 均通过） | 用户批准 | v2 移除了低层装饰器；注解方式保留 schema 与 `arg_default` 同源；关闭结构化输出避免客户端看到新增 `structuredContent` | 恢复 `mcp>=1.6.0,<2` 与 server.py/server_tools.py 旧实现 |
| 2026-11-04 | D3 | 替换 `/Applications/Utility.app` 前先退出所有 app 实例（含 `-a claude` 后台实例），否则旧实例会在 MCP 重启时用自身 bundle 的旧 wheel 覆盖生产 venv | Agent | 实测：新 app 首启后 venv 的 server.py 与备份 venv 字节相同、mcp 回落 1.28.1；仅替换 app 不做实例清理会留下「app 新版 + venv 旧版」的混合状态 | 重装 app 内 wheel 与 constraints 后只重启 MCP 子进程即可恢复 |
| 2026-11-10 | D1 | 记忆神经网 v2 允许破坏性变更；归属与强度分别以 `neuron_members`、`memory_activation` 为单一真相，`episode_id` 降为兼容投影 | 用户 | 用户明确“允许破坏性变更，结构必须合理”；双真相会让治理同步与回填长期漂移 | 保留 `episode_id` 投影与旧表，可回退到 v1 读取路径 |
| 2026-11-11 | D1 | 后台自动化 LLM 任务的输入禁止正文截断：去掉 `PROMPT_FACT_CHARS=400`（关联审查样本）与 `TEXT_LIMIT=4000`（治理候选正文），改为完整正文 + 按条数/字符分批；超预算只减少条数（留待下一轮），不切内容 | 用户 | 用户明确要求「后台自动化 LLM 等任务应保证数据完整」；截断会让模型在残缺事实上做合并/关联判断 | 恢复常量即可回到截断行为 |
| 2026-11-11 | D3 | 真实库 16 轮（112 天）演化验证生命周期收敛：active 16 / warm 0 / archive 5580，神经元与成员数全程不变（4217/5596，0 orphan） | Agent | 验证分层与衰减在真实数据上单调收敛且无漂移 | 只改副本，原库未动 |
| 2026-11-11 | D1 | 网络维护的全部同步重活移出事件循环：daily/weekly 外层、`govern_daily` 内部的 `build_candidates` / `_load_active_nodes` / `_apply_decisions`，以及 `score_relevance` / `forget_expired` / `VACUUM` 统一走 `asyncio.to_thread` | Agent | VM 实测 daily 期间 health 出现 8.712 秒停顿；移出后 max 204 ms | 保留同步调用版本即回到旧行为 |
| 2026-11-11 | D2 | 共激活接入召回：每次 recall 对 top 结果中前 5 个不同神经元两两调用 `coactivate`，无突触时建 0.06 弱边，跳过 notes 行 | 用户 | 用户要求把已实现未接线的 `coactivate` 加上；弱边低于扩散 floor 0.15，不污染检索，反复共召回才逐步加权 | 删除 `server_memory.py` 中的调用即可 |
| 2026-11-11 | D3 | `--drop-network` 删除网络表后把 `user_version` 置 0，下次连接自动重建 | 用户 | 用户要求「应能重建」；原实现写入当前版本号导致表永远不会重建 | 改回写 `SCHEMA_VERSION` 即恢复旧语义 |
| 2026-11-19 | D1 | recall 的 `filter_fallback` 补齐是用户定下的设计：project 硬过滤无内容时允许被过滤行回填，按维度标注；禁止把补齐机制本身当缺陷 | 用户 | 用户明确纠正审查报告的 P2 定性；`e2a2c2c` 已写明回填语义 | 保留 `_fallback_candidates` 与补齐标注 |
| 2026-11-19 | D2 | 记忆 project 归属由写入侧按 `source_files` 兜底推断，历史回填只补空值，无路径不写占位 | Agent | 6660/7674 active 节点缺 project，硬过滤命中不到内容；推断依据必须可追溯到路径 | 删除 `project_infer` 调用并停止回填脚本 |
| 2026-11-19 | D3 | 冷层 `record_cold_memory` 的 importance 由 REST 调用方透传；测试日志隔离必须在 import 前设置 `GATEWAY_CACHE` | Agent | 冷层 1277 条全为默认 5；`core/log_utils` 在 import 时固化缓存根 | 恢复固定 5；删除 conftest 的 env 设置 |
| 2026-11-19 | D3 | 部署 wheel 必须 `pip install --force-reinstall`，重启 Utility.app 不会更新 venv；旧 wheel 先备份 | Agent | 实测 app 重启后 venv 仍加载 00:55 旧 wheel，`project_infer` 不存在 | 从 `/tmp/gateway-wheel-<日期>/` 恢复旧 wheel 并重启子进程 |
| 2026-11-19 | D2 | recall 的单关键词变体按需触发：base 两路去重后不足 limit 才跑，最多 3 路 | Agent | 常驻 3 路使 p50 3057ms；按需后 2899ms 且命中率不变 | 删除 `_term_variants` 调用 |
| 2026-11-19 | D2 | rerank 负分丢弃只从 strict 行回填，不复活 floor 补充分 | Agent | 全量回填时无内容查询命中从 18.8% 升到 50%；仅 strict 回填后误报不增 | 删除 `strict_dropped` 回填块 |
| 2026-11-19 | D2 | 实体臂 ASCII 名按词边界匹配，纯符号排除；CJK 仍子串匹配 | Agent | 随机串 `zzz-nonexistent-abc` 因实体 `en`、`-` 召回 10 条 | 恢复 `instr` 子串匹配 |
| 2026-11-19 | D3 | hot floor 暂维持 0.616；0.55 在矩阵上提升命中但会与 rerank 兜底叠加放大弱结果 | Agent | 评估数据见 `.agent/plans/archive/2026-11-19-recall-recallability/plan.md` | 把 `retrieval_thresholds.json` 的 hot 改为 0.55 |
| 2026-11-19 | D2 | 候选：floor 改由 gold 249 条完整查询的 dense 分数离线校准，自校准只做向量一致性体检；生产阈值暂不动 | Agent | 全量正文自检索恒等 1.0（冷层 1297 条 self p50=1.0）；gold 数据见计划 P0 实证 | 恢复 20 字探针会重新引入截断，禁用 |
| 2026-11-19 | D2 | cold/expanded floor 0.569→0.50，hot 0.616 与 notes_semantic 0.37 不变 | 用户批准写入生产 | 生产 trace 冷层最终出现率 3.2%→21.1%；形态矩阵命中 100%、误报 0、p50 2.8s | 恢复 `retrieval_thresholds.json.bak-cold050-20261119T175105` 即回退 |
| 2026-11-19 | D2 | 候选：部署 filter_fallback 修复与冷层 project 推断，并回填生产冷层 573 条 | Agent | source_file 可推断 44% 冷层 project；无来源/冲突的 724 条保持空 | 回填备份 JSON 可逐条恢复项目字段 |
| 2026-11-20 | D2 | 写入侧分组继承改为近邻多数投票，阈值 0.80→0.65 | 用户批准 | 4200/4784 组为单例，今日新增 243 条中 112 条仍单例；原逻辑只看 top-1 | 恢复 `EPISODE_INHERIT_SIMILARITY=0.80` 并改回单条继承 |
| 2026-11-20 | D2 | 周任务新增全量分组治理回扫（`scope_all`），日任务保持 24h 增量 | 用户批准 | 日任务只覆盖近 24h 触及的组，历史碎片永不重扫 | 去掉 `run_weekly` 中的 `govern_daily(scope_all=True)` |
| 2026-11-20 | D3 | 治理候选注入 session/task/file，提示词改为同 session 或同 task 即同一次任务 | 用户批准 | 候选原无任务信号，LLM 无法判断「同一次任务」，只能保守 keep | 恢复旧 `render_candidates` 与提示词 |
| 2026-11-20 | D2 | `neurons.importance` 由成员节点 importance 均值/10 聚合（0-1），随每日网络维护刷新 | 用户批准 | 5635 个神经元全为 0，重放排序退化 | 删除 `network.refresh_importance` 调用 |
| 2026-11-20 | D2 | 重放种子只取有活跃突触的神经元 | 用户批准 | 仅修 importance 仍会抽到孤立节点；副本验证 80 个种子全部有边 | 删除 `sample_seeds` 的 `EXISTS` 条件 |
| 2026-11-20 | D3 | notes 预热线程在 macOS 降为 Background QoS；宿主不再在 `-a` 启动时调用 prewarm | Agent | 启动预热让每次会话单核满载 5~10 分钟并与 IDE 抢 P 核；后台 QoS 由系统调度到能效核，查询路径原本就不阻塞（`NOTE_QUERY_EMBED_BUDGET=0`） | 删除 `_apply_background_qos` 调用；宿主恢复启动预热请求 |
| 2026-11-20 | D2 | trace_id 为请求级 contextvar：代理中间件与 MCP `_dispatch_tool` 各自生成绑定，检索 trace 用 `current_or_new()` 继承；两表新增可空 `trace_id` 列，旧库启动时 `ALTER` | 用户批准 | 代理与工具调用此前无法关联同一次请求；118699 行真实库副本迁移后旧行 NULL、读取正常 | 删除 `trace_context` 调用与两处 ALTER；写入列回退为无 trace_id |
| 2026-11-20 | D1 | recall 结果分层：`results` 只含 floor+rerank 双通过的命中，可为空、不填满；低分/判负/被过滤行进 `candidates` 并带 `reason`；`status` 改 `ok`/`weak_match`/`candidates_only`/`empty`，不再用补齐行填 `results` | 用户批准 | 生产 11-20 trace 67% 返回行为 rerank 判负复活；VM 真实副本 249 条 gold A/B：specific 精度 4.5%→8.6%，入口覆盖 21.8%→23.5%，基线命中 38 条零丢失；基线 79 条「不空」查询平均 5.41 条仅命中 1 条 | 恢复 `pipeline.py` 的 weak/fallback 填 `top` 与 `rerank_fallback` 复活块；`server_memory.py` 删除 `candidates` 字段 |
| 2026-11-21 | D2 | 短查询词面救援（补充 D1 分层）：≤3 个空白分词的查询里，rerank 判负行按词面覆盖救援回 `results` 并标 `rerank_rescued`；N≤2 需全覆盖、更长查询允许缺 1 个词（CJK bigram 会跨词，75% 在 N=3 不可达），ASCII 词边界、CJK 子串、排除停用词；救援行固定排在 rerank 通过行之后，不参与 logit 混排 | Agent | 交叉编码器对短查询漂移 0.4~1.0，VM 真实副本 12/12 短查询补回 anchors、20 条不存在查询 0 anchors | 删除 `RERANK_RESCUE_*` 常量与 `_term_coverage` 调用，恢复判负行仅进 `candidates` |
| 2026-11-25 | D1 | 工具去重注记文案改为「本次工具调用结果与上一次工具调用结果相同，可直接复用，可复用工具结果 id: {tool_id}」，英文同步；占位符 `id` 更名 `tool_id` | 用户 | 用户指定文案并要求其他语种同步；未知 id 仍渲染 `?` | 恢复旧文案「本次运行结果与上次结果相同（工具结果 {id}）」与 `id=` 传参 |
| 2026-11-25 | D1 | GitHub Actions 保持关闭：11-05 后无新 run 是用户主动关闭，非平台故障；质量门以本地 ruff + unittest + build 为准 | 用户 | 用户确认「我关了」；`ci.yml` 的 memory extra 修复保留，供将来开启时使用 | 用户重新启用 Actions 后下次 push 即触发 |
| 2026-11-25 | D3 | 基座升级 1.6.0 → 1.6.1；UIX-06 因无 UI 跳过 | Agent | 按 upgrade-seeds 增量合并；`ui.has_ui: false` | 回退对应规则与技能文件，写回旧 seed_version |
| 2026-11-25 | D2 | 新增 `services/mesh`：文件注册表 + 目录/HTTP 传输 + Router + `/mesh/*` + 8 个 `service_*` 工具，不保投递不重试 | 用户 | Utility 需要跨工作区/跨机 Agent 通信；A2A 概念参考但不实现 JWS/OAuth/JSON-RPC | 删除 `services/mesh`、`register_mesh_tools` 与 server.py 的 `/mesh` 路由挂载 |
| 2026-11-25 | D2 | control start 绕过存活门槛；单卡接口返回过期卡；`/mesh/services?remote=0` 防 peers 递归；API/MCP 网络调用走线程池 | Agent | 远程 `service_start` 曾恒 `target_unknown`；peers 列表互相递归阻塞事件循环 | 恢复存活门槛、单卡隐藏过期卡、peers 列表默认聚合；去掉线程池包装 |
| 2026-11-25 | D1 | review 修复：非回环 bind 强制 token 并保护 REST/MCP；register/deregister 仅本机；endpoint 限制 inbox 根；heartbeat status 校验；远端错误码保真；control start 需 startable；工具与 API 全线程池 | 用户 | review 报告 P0：整站无鉴权、任意目录写、远端卡本地投递、卡状态可被破坏 | 恢复旧中间件/校验，去掉线程池包装 |
