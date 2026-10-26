# 项目状态

## 当前目标

完成项目基座（init 产物），随后进入 G2：让 macOS 宿主改用 wheel 分发。

## 进行中

| 任务 | 状态 | 阻塞 |
| --- | --- | --- |
| （无） | — | — |

## 已完成

- 2026-10-26 基座生成并由用户确认（.agent、6 个技能、契约、CI）
- 2026-10-26 从 utility 仓迁入 Python 能力层，import 改为 `agent_gateway`；`PYTHONPATH=src python -m unittest discover -s tests -t .` → 128 例通过（1 skipped）
- 2026-10-26 `agent-gateway --check` 在真实与临时配置下通过
- 2026-10-26 wheel 构建与干净 venv 安装验证通过，资源（prompts / ast-js）指向 site-packages
- 2026-10-26 `--export-contracts` 产出 `contracts/env.json`（39 个 env 键）
- 2026-10-26 采用 src layout（`src/agent_gateway/`），pyproject 与测试路径同步
- 2026-10-26 lint 基线清零（641 → 0）：删除未使用导入，兼容再导出模块加 per-file-ignores，CI lint 转阻塞门

## 下一步

1. G2：宿主打包改用 wheel（在 utility 仓执行）

## 最近验证

| 时间 | 命令 | 结果 |
| --- | --- | --- |
| 2026-10-26 | `ruff format --check src tests scripts` | 通过（153 文件） |
| 2026-10-26 | `ruff check src tests scripts` | 通过（0 条） |
| 2026-10-26 | `.venv/bin/python -m unittest discover -s tests -t .` | 通过（128，1 skipped） |
| 2026-10-26 | `.venv/bin/python -m build` | 通过（wheel + sdist） |
| 2026-10-26 | `UTILITY_DATA_DIR=<临时根> agent-gateway --check` | 通过 |

## 已知问题

- `test_ast_imports` 中调用主项目二进制的用例被跳过：本仓没有 `utility` 可执行文件，属预期降级。
- `pip-audit` 尚未安装，交付门暂未执行。
