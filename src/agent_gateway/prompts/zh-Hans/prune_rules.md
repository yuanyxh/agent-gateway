---
name: prune_rules
placeholders: [level, limit, items]
json_keys: [keep_indexes]
---
规范文件「{level}」条目超过上限 {limit} 条，需要淘汰复用价值最低的条目。

现有条目：
{items}

要求：保留最有复用价值、最能指导行动的 {limit} 条，淘汰过时/低复用/重复的。

返回 JSON：{"keep_indexes": [保留的条目下标数组]}
只返回严格标准的 JSON 对象，不要代码块包裹。
