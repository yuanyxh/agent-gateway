# agent-gateway

自包含的 AI 能力层：MCP 记忆、LLM 网关、token 代理、项目图与桌面自动化服务。Utility 是客户端之一，不是运行前提。

形态：library　技术栈：Python / 无框架　运行期：Python ≥3.12

## 开始任何任务前

1. 缺少 `.agent/profile.yaml` 时先加载 `init`，不得直接选技术栈或写代码。
2. 读一次 `.agent/status.md` 的当前目标与阻塞；有活动计划时读 `.agent/plans/` 对应文件，未排期事项读 `.agent/backlog.md`；不要遍历 `.agent/`。
3. 按下方加载策略取用本次任务命中的内容，未命中的不读。
4. 做技术选型、升级、引入新依赖或新增技能前，读 `.agent/research.md` 的对应结论与来源；依赖升级另读 `.agent/upgrades.md`。

## 常驻约束

<!-- core:start -->
1. **单一事实源**：目标与进度摘要只写 `.agent/status.md`，执行细节只写当前计划，未排期事项只写 `.agent/backlog.md`，决策只写 `.agent/decisions.md`；其他文档引用不复制。
2. **事实优先**：代码、测试、构建输出与运行日志高于记忆；结论附命令输出或文件路径。
3. **授权边界**：提交、推送、发布、删除、产生费用或外发数据前必须取得用户确认。
4. **决策分级**：目标/范围/预算由用户定；技术选型与实现细节由 agent 决定并记入决策日志；只在不可逆、产生费用、隐私合规、目标冲突时暂停。
5. **凭据不入库**：密钥只从 `agent-gateway.yaml` 读取，不进入源码、日志与提交记录。
6. **删除可恢复**：删除文件或数据优先移到废纸篓。
7. **仓边界**：本仓只做能力层。服务端与 Agent 循环在 workspace 仓（Node/TS），禁止把服务端或移动端代码加入本仓。
8. **配置唯一来源**：`agent-gateway.yaml`；env 只做覆盖；任何服务都必须能在没有宿主注入时独立启动。旧宿主形状只经 `core/config_compat.py` 翻译，保留一个发布周期。
9. **跨仓契约用文件**：消费方读 `contracts/*.json`，不扫描源码。
10. **变更后沉淀**：修复、发布、重构或用户纠正后运行 `learn`，无新事实不记录。
11. **周期体检**：报告过期或增删 agent 资产前运行 `steward`，不触碰业务代码。
12. **先隔离再 E2E**：集成与端到端验证前用 `sandbox` 建一次性环境，禁用生产凭据与数据。
13. **用户纠正优先**：记忆、规则与用户当次修正冲突时停止确认，旧条目标记 superseded。
14. **自动化输入完整性**：LLM、质量门、分组、矛盾、合并与评估的输入必须用原始正文，禁止用 `title`、`snippet`、摘要切片等展示字段；截断必须命名预算并标 `truncated`/`degraded`。
<!-- core:end -->

## 宿主接入契约

- 每个可被宿主拉起的模块必须暴露**同步** `main()`；异步内部逻辑放 `async def serve()`，`main()` 里 `asyncio.run(serve())`。
  宿主按模块名启动：`python -c "import sys; from <module> import main; sys.exit(main())"`，
  **没有脚本路径**，所以返回码要能通过 `sys.exit(main())` 传出去（int 返回值 = 退出码）
- 当前入口：`services.mcp.server` / `services.proxy.token_proxy` / `services.summarize.summarize` /
  `services.knowledge.knowledge_refill` / `scanner.ast_extractor`
- 宿主随包分发本仓 wheel，首启装进 venv；依赖只从 wheel extras（`proxy,memory,server`）来，不另发 `requirements.txt`
- 提示词与 `scanner/ast-js` 必须留在包内（wheel 里），宿主读的是打包时拷出的副本；新增或修改提示词后，utility 仓 `Sources/Gateway/prompts/` 副本必须同步
- i18n 语言包自持：`src/agent_gateway/locales/<lang>.json`（扁平 `{key: text}`），
  `core/l10n.py` 启动时加载；不再由宿主生成，两仓各自维护自己的语言包

## 加载策略

| 内容 | 层 | 触发 | 机制 |
| --- | --- | --- | --- |
| 本契约与常驻约束 | L0 | 每次会话 | 平台自动加载 |
| `.claude/skills/` | L1 | 任务命中技能描述 | 平台按 description 触发 |
| `.agent/rules/` | L2 | 任务命中规则范围 | 技能正文引用后读取 |
| `.agent/domains/` | L3 | 涉及能力层关注点 | 按需读取 |
| `.agent/knowledge/` | L3 | 命中索引主题 | 每次 1~3 篇 |
| `.agent/decisions.md`、`lessons.md` | L3 | 需要历史依据或复盘 | 按需读取 |
| `.agent/plans/`、`.agent/backlog.md` | L3 | 多步骤任务或选择下一项工作 | 按需读取 |
| `.agent/upgrades.md` | L3 | 依赖升级或安全公告 | 按需读取 |
| `.agent/environment.md` | L3 | 安装、构建失败或换环境 | 按需读取 |
| `.agent/research.md` | L3 | 技术选型、升级或引入新依赖 | 按需读取 |
| `.agent/memory/` | L3 | 需要跨会话结论 | 按需读取 |

不把规则、画像、状态或技能正文复制进本文件。常驻总量 ≤ 200 行。`.agent/rules/core.md` 是主题规则索引；开始任务、做决策、澄清需求或执行不可逆操作前，按索引读取对应文件。

## 技能索引

| 技能 | 触发 |
| --- | --- |
| `init` | 缺少画像、从零开始或初始化 |
| `build` | 实现需求、继续开发 |
| `review` | 审查变更、合并前检查 |
| `hunt` | 报错、崩溃、回归 |
| `learn` | 沉淀经验、复盘 |
| `upgrade` | 依赖与运行时升级 |
| `delegate` | 子 agent、并行工作者或跨会话委派 |
| `operate` | 运行状态、告警、成本与用户反馈 |
| `steward` | agent 环境体检、规则/记忆整理 |
| `sandbox` | 隔离环境、端到端验证 |

未生成 `ship`：当前 `deploy_target: none`，发布流程随 workspace 开工后再生长；`deploy` 规则同理跳过，`ui` 规则因 `has_ui: false` 跳过。

## 工作循环

计划 → 实现 → 自测 → 审查 → 更新状态与待办。当前阶段无发布与线上运维。

## 提交规范

- 一个提交只做一件事；信息格式 `类型(范围): 摘要`，类型限定 feat / fix / refactor / test / docs / chore / perf / build。
- 提交、推送、合并与改写历史均需用户明确授权；未授权时只给建议与命令。
- 不提交生成物、依赖目录、本地配置、凭据与验证数据。

## 工程化卡口

| 检查 | 命令 |
| --- | --- |
| 格式化 | `ruff format --check src tests scripts` |
| 静态检查 | `ruff check src tests scripts` |
| 测试 | `PYTHONPATH=src python -m unittest discover -s tests -t .` |
| 构建 | `python -m build` |
| 自检 | `agent-gateway --check` |
| 版本检测 | `pip list --outdated` |
| 漏洞扫描 | `pip-audit`（未安装时登记降级） |

本地与 CI 执行同一组命令；CI 见 `.github/workflows/ci.yml`。

本地钩子：`git config core.hooksPath .githooks`。`pre-commit` 执行 ruff format/check，`commit-msg` 校验 `类型(范围): 摘要`。

## 断点规则

只有四种情况暂停等待用户：不可逆操作、产生费用、涉及隐私或合规、目标之间冲突。其余按画像与规则默认推进，并记入决策日志。

## 验证命令

| 用途 | 命令 |
| --- | --- |
| 安装 | `pip install -e ".[proxy,memory,server]"` |
| 构建 | `python -m build` |
| 格式化检查 | `ruff format --check src tests scripts` |
| 静态检查 | `ruff check src tests scripts` |
| 测试 | `PYTHONPATH=src python -m unittest discover -s tests -t .` |
| 运行 | `agent-gateway --check` |
| 版本检测 | `pip list --outdated` |
| 契约导出 | `agent-gateway --export-contracts` |
| 独立自检 | `GATEWAY_HOME=$(mktemp -d) agent-gateway --check` |
| 建图 | `agent-gateway graph index --root /tmp/demo --json` |
