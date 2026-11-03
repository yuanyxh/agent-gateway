# 任务计划：基座升级到 agent-vibe 1.4.0

状态：done
更新：2026-11-03
分支：master

## 目标

把 `.agent/profile.yaml` 的 `seed_version` 从 1.0.0 升到 1.4.0，按增量合并规则、技能与文档。

## 非目标

- 不升级第三方依赖；候选项只写入 `.agent/upgrades.md`。
- 不生成 `ship`、`deploy`、`ui`：画像为 `deploy_target: none`、`has_ui: false`。
- 不提交、不推送、不部署。

## 验收

- `verify-instantiation.sh` 为 0 errors。
- `ruff format --check`、`ruff check`、unittest 通过。
- 既有项目定制不被覆盖；跳过项有记录。

## 约定

| 日期 | 约定 | 来源 |
| --- | --- | --- |
| 2026-11-03 | 合并而非覆盖；冲突登记到 decisions | upgrade-seeds 协议 |
| 2026-11-03 | 用户确认升级基座 | 用户 |

## 步骤

| 编号 | 步骤与依赖 | 状态 | 证据 |
| --- | --- | --- | --- |
| S1 | 读取 1.0.0→1.4.0 变更记录与校验结果 | 完成 | CHANGELOG 1.1~1.4；校验 3 errors |
| S2 | 补 backlog、upgrades、plans 模板 | 完成 | 3 个文件存在且非空 |
| S3 | 画像补 lockfile、trunk_branch、outdated | 完成 | profile.yaml |
| S4 | 规则合并 CTX-04~06、VCS-07、observability | 完成 | rules/*.md |
| S5 | 技能合并 build/review/init/learn/hunt/upgrade，新增 delegate/operate | 完成 | .claude/skills/ |
| S6 | 更新启动契约与规则索引 | 完成 | CLAUDE.md、rules/core.md |
| S7 | 更新 seed_version 并校验 | 完成 | 0 errors、1 warning |
| S8 | 回归测试与静态检查 | 完成 | 502 tests、ruff 通过 |

## 偏移记录

| 日期 | 变更与类型 | 原因 | 影响与决定 |
| --- | --- | --- | --- |
| 2026-11-03 | 跳过 ship/deploy/ui | 画像不适用 | 登记于 backlog 与 status |
| 2026-11-03 | lockfile 声明 none | 项目按 pyproject extras 安装，无锁定文件 | 校验保留 1 条 warning |
| 2026-11-03 | 新增 delegate/operate | `applies_when` 命中当前画像 | 技能索引同步 |

## 交接

- 当前位置：S8 完成。
- 下一步动作：用户授权后按主题提交与部署。
- 未决问题：依赖升级批次、lockfile 策略。
