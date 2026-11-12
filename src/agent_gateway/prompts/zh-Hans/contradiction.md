---
name: contradiction
placeholders: [items_text, content]
json_keys: [relation]
---

你是一个记忆一致性检测器。请判断以下"新记忆"与"已有记忆"之间的关系。

已有记忆：
{items_text}

新记忆：
{content}

请判断关系类型并返回 JSON:
- "contradiction": 新记忆与某条已有记忆在事实上矛盾
- "supplement": 新记忆补充或细化了已有记忆, 可以共存
- "duplicate": 新记忆与已有记忆高度重复, 建议合并
- "unrelated": 完全不同的话题

同时从新记忆中抽取可长期保留的语义层:
- "entities": 命名实体列表, 每项 {"name", "type", "aliases"}; type 取值 person, project, system, file, concept, place, organization
- "facts": 主谓宾三元组列表, 每项 {"subject", "predicate", "object", "confidence"}; 只在新记忆陈述稳定事实时填写

返回 JSON: {"relation": "<类型>", "reason": "<一句话原因>", "conflicting_id": "<冲突的记忆ID, 仅 contradiction 时填写>", "entities": [...], "facts": [...]}
只返回严格标准的 JSON 对象, 不要代码块。