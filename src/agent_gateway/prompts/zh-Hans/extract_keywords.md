---
name: extract_keywords
placeholders: [SUMMARY_KEYWORD_COUNT, topic, facts, SEARCH_MIN_TOKEN_LEN]
json_keys: []
---
从以下记忆主题中提炼 3-{SUMMARY_KEYWORD_COUNT} 个用于检索知识库的关键词。

主题：{topic}
相关事实：
{facts}

要求：
1. 名词性关键词：专有名词、项目名、技术栈、库名、主题中心词
2. 每个关键词 ≥{SEARCH_MIN_TOKEN_LEN} 字符
3. 避免「记忆」「总结」「问题」「修复」「开发」等通配词
4. 用具体词而非描述句
5. 数量 3-{SUMMARY_KEYWORD_COUNT} 个

返回 JSON：{"keywords": ["关键词1", "关键词2", ...]}
只返回严格标准的 JSON 对象，不要代码块包裹。