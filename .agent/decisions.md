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
