---
name: entity_extract
placeholders: [memories_text]
json_keys: [memories]
---

你是记忆实体抽取器。下面是若干条记忆，每条以 [id] 开头。

{memories_text}

为每条记忆抽取实体与事实。只使用原文出现的信息，不要推断，不要翻译人名。

返回 JSON:
{"memories": [{"id": "<原 id>", "entities": [{"name": "<原文写法>", "type": "person|place|food|organization|concept|other", "aliases": ["<其他写法>"]}], "facts": [{"subject": "<实体名>", "predicate": "<简短动词或关系>", "object": "<实体名或值>", "polarity": "positive|negative|neutral", "confidence": 0.0}]}]}

要求：
- 每条记忆都要出现在返回里，没有实体时给空数组
- subject 与 object 优先使用本记忆列出的实体名
- confidence 取 0.0 到 1.0
只返回严格标准的 JSON 对象, 不要代码块。
