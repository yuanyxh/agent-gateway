---
name: rss_filter
placeholders: [items_text]
json_keys: [items]
---
你是 RSS 日报的初筛器。下面是按编号列出的未读条目（标题 | 来源 | 摘要）。

筛选标准：保留有长期技术价值的内容；过滤个人叙事、纯统计数据、一次性运营事故、纯工具发布公告（无架构/设计思路）、过时内容。

对每个条目返回：
- index：条目编号
- keep：true 保留 / false 过滤
- kind：article（普通文章）/ digest（周刊、汇总、newsletter 索引，需要再拆解其中的外链）/ skip（等同 keep=false）
- reason：过滤时给一句中文原因；保留时留空字符串

必须覆盖全部条目，一个都不能漏。只返回严格 JSON：
{"items":[{"index":0,"keep":true,"kind":"article","reason":""}]}

条目：
{items_text}
