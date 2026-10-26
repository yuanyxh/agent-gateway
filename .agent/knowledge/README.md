# 知识索引

按任务主题加载，单次 1~3 篇，不整册注入。知识包来自 agent-vibe 的通用主题，与项目规则冲突时以项目规则为准。

| 主题 | 触发条件 | 来源 |
| --- | --- | --- |
| 配置化 | 新增配置项、修改优先级、配置分散 | `seeds/knowledge/configuration.md` |
| 测试策略 | 新增测试层、质量门调整、回归设计 | `seeds/knowledge/testing.md` |
| 异常与轨迹 | 需要错误码、日志字段、问题定位 | `seeds/knowledge/observability.md` |
| 代码设计与模式 | 复制代码增多、需要抽出公共层 | `seeds/knowledge/design-patterns.md` |
| 异步任务与队列 | 引入后台任务、重试、批量处理 | `seeds/knowledge/async-jobs.md` |
| 部署自动化 | workspace 开工、容器化 | `seeds/knowledge/deployment.md` |

## 项目内沉淀

本项目已验证的模式写入 `.agent/lessons.md`，重复出现且跨项目可用时再提议晋升到种子库。
