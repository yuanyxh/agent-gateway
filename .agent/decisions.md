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

## 记录规则

- D1、D2 必须由用户决定，记录用户原话要点。
- D3 由 Agent 决定，记录默认值来源。
- 决定被推翻时新增一条，不删除旧记录。
