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

## 记录规则

- D1、D2 必须由用户决定，记录用户原话要点。
- D3 由 Agent 决定，记录默认值来源。
- 决定被推翻时新增一条，不删除旧记录。
