# 项目状态

## 当前目标

完成项目基座（init 产物），随后进入 G2：让 macOS 宿主改用 wheel 分发。

## 进行中

| 任务 | 状态 | 阻塞 |
| --- | --- | --- |
| G2 宿主接入 | wheel 分发与模块启动已在 utility 侧验证；等本仓改动提交 | — |
| 记忆 episode_id 分组 | 阶段 1/2/3 完成；cold 扩展与新写入分组已实现，待打包部署 | — |

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

## 下一步

1. 提交本仓改动（模块入口契约 + 迁入的检查脚本）
2. utility 侧打包与整机回归通过后发版

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

## 已知问题

- `test_ast_imports` 中调用主项目二进制的用例被跳过：本仓没有 `utility` 可执行文件，属预期降级。
- `pip-audit` 尚未安装，交付门暂未执行。
- 冷层 774 条（created_at ≤ 2026-10-22）缺 `source_memory_ids`：热层 processed 在 10-23 批量迁移后丢失消费日期，历史来源不可恢复，决定不回填（见 decisions 2026-10-28）。消费方 `_mark_cold_stale` 对缺键与空列表行为一致，无功能影响。
