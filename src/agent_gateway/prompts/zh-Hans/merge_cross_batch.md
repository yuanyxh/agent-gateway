---
name: merge_cross_batch
placeholders: [entries]
json_keys: []
---
以下为分批聚合得到的记忆组，跨批可能存在主题重复。请将 topic 相同或相近的组收敛为一组。

记忆组：
{entries}

要求：合并后 facts 按时间线组织（先发生在前），同一主题跨批演进时保留演变过程、关键节点保留日期标注。

返回 JSON：{"groups": [{"topic": "合并后主题", "facts": "合并后按时间组织的完整事实文本", "tags": ["标签"], "retain": true}]}
只返回严格标准的 JSON 对象，不要代码块包裹。