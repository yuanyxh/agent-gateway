---
name: consolidate
placeholders: [facts_text]
json_keys: [summary]
---

你是记忆整合器。下面这些事实属于同一个主题簇。

{facts_text}

把它们合并成一条事实：保留每个不同的细节和每个尚未解决的冲突。不要编造事实，不要添加建议，不要重复同一条事实。

返回 JSON: {"summary": "<一条事实>", "context": "<适用场景以及这个簇为什么重要>"}
只返回严格标准的 JSON 对象, 不要代码块。
