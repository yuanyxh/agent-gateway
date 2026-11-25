# MCP SDK v2 升级基线（2026-11-04）

## 测试与静态检查

| 项 | 结果 |
|---|---|
| 命令 | `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -t .` |
| 结果 | `Ran 553 tests ... OK` |
| 耗时 | 6.8 s |
| ruff format | 250 files already formatted，exit 0 |
| ruff check | All checks passed，exit 0 |

## 工作区状态

| 仓 | 状态 |
|---|---|
| agent-gateway | 仅 `.agent/plans/2026-11-04-mcp-sdk-v2-upgrade/` 未跟踪；其余干净 |
| utility | 干净 |

回滚补丁：两仓当前无已跟踪文件改动，`git diff` 为空；依赖声明回滚以本文件记录的版本表为准。

## 开发 venv（.venv）

| 包 | 版本 |
|---|---|
| mcp | 2.3.0 |
| mcp-types | 2.3.0 |
| httpx | 0.28.1 |
| httpx2 | 2.16.0 |
| opentelemetry-api | 1.45.1 |
| starlette | 1.10.0 |
| uvicorn | 0.54.0 |
| anyio | 4.15.1 |
| pydantic | 2.14.1 |
| mem0ai | 2.2.1 |
| fastembed | 0.9.1 |
| qdrant-client | 1.20.0 |
| numpy | 2.5.4 |
| tree-sitter | 0.25.2 |
| trafilatura | 2.3.1 |
| aiohttp | 3.14.5 |
| Brotli | 1.2.1 |
| pathspec | 1.1.1 |
| PyYAML | 6.0.3 |

## 生产 venv（~/.config/utility/mcp-venv）

| 包 | 版本 |
|---|---|
| mcp | 1.28.1 |
| httpx | 0.28.1 |
| httpx-sse | 0.4.3 |
| starlette | 1.3.1 |
| uvicorn | 0.51.0 |
| anyio | 4.14.1 |
| pydantic | 2.13.4 |
| mem0ai | 2.0.11 |
| fastembed | 0.8.0 |
| qdrant-client | 1.18.0 |
| numpy | 2.5.1 |
| tree-sitter | 0.25.2 |
| trafilatura | 2.3.1 |
