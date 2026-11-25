# MCP SDK v2 与全量依赖升级 任务清单

- [x] T1 基线固化（依赖：无，规模：XS）
- [x] T2 依赖地板（依赖：T1，规模：S）
- [x] T3 MCPServer 骨架与 ASGI 装配（依赖：T2，规模：M）
- [x] T4 14 个工具迁移（依赖：T3，规模：L）
- [x] T5 skills 与错误语义回归（依赖：T4，规模：S）
- [x] T6 Web 栈升级（依赖：T5，规模：S）
- [x] T7 解析栈升级（依赖：T6，规模：M）
- [x] T8 记忆栈升级（依赖：T7，规模：M）
- [x] T9 其余 Python 依赖（依赖：T8，规模：S）
- [x] T10 Swift SPM 依赖（依赖：T9，规模：M）
- [x] T11 打包与端到端（依赖：T10，规模：M）

## 检查点 A（T1-T5）

- [x] 566 测试全绿（基线 553，新增 13）
- [x] `ruff format --check` / `ruff check` 通过
- [x] 14 个工具 schema 与旧 `build_tools()` 逐项一致
- [x] 根路径 MCP、`/health`、`/api/*` 三条入口通过

## 检查点 B（T6-T10）

- [x] Python 依赖全部到达目标版本
- [x] `swift build` 通过
- [x] `bash scripts/run-tests.sh` 通过

## 检查点 C（T11）

- [x] wheel 装入生产 venv，constraints 重新生成（mcp==2.3.0）
- [x] SDK Client 实测：2026-07-28 握手、29 个工具、工具调用成功
- [x] 2026-07-28 实测结论写入 `.agent/decisions.md`
- [x] Utility.app 首启不产生 `.agent-gateway-broken`
- [x] Claude Code 侧 `/mcp` 重连后工具列表与 `remember` / `recall` 调用（2026-11-04 已验证）
