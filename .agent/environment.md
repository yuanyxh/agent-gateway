# 环境探测

## 2026-10-26（初次）

| 项 | 状态 | 版本 / 说明 | 替代或影响 |
| --- | --- | --- | --- |
| 平台 | 可用 | macOS 27.0.1 arm64 | 无 |
| Python | 可用 | 3.12.13（项目 venv） | 无 |
| pip | 可用 | 26.1.2 | 无 |
| 虚拟环境 | 可用 | 项目内 `.venv`（开发）与宿主 venv（运行期） | 无 |
| 构建后端 | 可用 | hatchling（构建时由 PEP 517 隔离安装） | 无 |
| 格式化 / 静态检查 | 可用 | ruff 0.17.0 | 无 |
| git | 可用 | 2.55.0 | 无 |
| Node / npm | 可用 | v24.16.0 / 11.13.0 | 仅 scanner 的 `ast-js/parse.mjs` 需要；缺失时该分支降级 |
| Docker | 缺失 | 未安装 | 当前 `deploy_target: none`，部署验证留到 workspace 开工 |
| pip-audit | 缺失 | 交付门依赖它 | 首次执行前安装；未安装时登记为降级，不阻塞本地开发 |

## 2026-10-26（复验）

| 项 | 状态 | 说明 |
| --- | --- | --- |
| 项目 venv | 已重建 | 复验时 `.venv` 缺失，用 `python3.12 -m venv .venv` 重建 |
| 依赖 | 已安装 | `.venv/bin/pip install -e ".[proxy,memory,server]"` |
| 构建工具 | 已安装 | `.venv/bin/pip install build`（非运行依赖） |
| 系统 PATH | 注意 | `python` 不存在，需用 `.venv/bin/python` 或 `python3.12` |

## 判定

- 质量门 `ruff`、`unittest`、`python -m build` 在当前环境可执行。
- 可选 `ast-js` 解析依赖 Node；其余路径均为纯 Python。
- 容器化与远程发布暂不可验证，已登记，不作为当前阶段阻塞。

## 刷新规则

环境变化、命令失败或新会话开始时重新探测；旧记录保留时间戳，不覆盖历史。
