# 研究简报

检索日期均为 2026-10-26。

## 包布局

| 结论 | 来源 | 采纳 |
| --- | --- | --- |
| src layout 防止「cwd 中的源码副本掩盖打包遗漏」；代价是 CLI 不能直接从源码树运行，需 editable 安装 | Python Packaging User Guide — src layout vs flat layout（`packaging.python.org/en/latest/discussions/src-layout-vs-flat-layout/`，官方） | **采纳**：迁移到 `src/agent_gateway/` |

映射：`pyproject.toml` 的 `packages` 指向 `src/agent_gateway`；测试用 `PYTHONPATH=src`；宿主运行期使用 wheel，不受影响。

## 测试框架

| 结论 | 来源 | 采纳 |
| --- | --- | --- |
| 新项目 2026 年默认推荐 pytest；pytest 原生运行 unittest 用例 | qaskills.sh《unittest vs pytest 2026》（2026-05-22，非官方博客） | **拒绝迁移**：现有 128 例使用 unittest，零额外依赖；未来需要 fixture / 参数化时再评估 |

## 打包与依赖

| 结论 | 来源 | 采纳 |
| --- | --- | --- |
| PEP 621 + hatchling 是当前主流后端，可选依赖分组用 extras | PyPA 指南与仓库现状 | 维持现状（hatchling + `proxy`/`memory`/`server` extras） |

## 安全（画像 security_level = high）

| 结论 | 来源 | 采纳 |
| --- | --- | --- |
| 凭据只通过配置或密钥管理注入，不写入源码、日志与提交记录 | 项目已有规则（SEC-02） | 已执行：密钥仅从 `config.yaml` 读取 |
| 交付前做依赖漏洞扫描 | 常见供应链实践 | 采纳：`pip-audit` 纳入交付门（当前未安装，登记降级） |

## Agent 资产盘点

| 资产 | 位置 | 结论 |
| --- | --- | --- |
| 用户级技能 | `~/.claude/skills`（不存在） | 无可复用或需去重的技能 |
| 用户级规则 | `~/.claude/rules`（不存在） | 无 |
| 项目级技能/规则 | 本仓初始化前无 `.claude` | 由 init 生成 |
| MCP 工具 | mino-brain（记忆/搜索/读网页）、yunxiao（研发流程）、ide | 直接使用，不复制其实现 |

## 未采纳清单

- 不引入 uv 作为构建前端（收益不足，现有 hatchling 链路已验证）。
- 不引入 mypy/pyright（`typecheck: none`，现有注解密度不足以产生价值）。
- 不引入容器化开发环境（本机无 Docker，画像 `deploy_target: none`）。
