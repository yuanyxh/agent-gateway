---
name: extract_keywords
placeholders: [SUMMARY_KEYWORD_COUNT, topic, facts, SEARCH_MIN_TOKEN_LEN]
json_keys: []
---
Extract 3-{SUMMARY_KEYWORD_COUNT} keywords for searching the knowledge base from the memory topic below.

Topic: {topic}
Related facts:
{facts}

Requirements:
1. Noun-form keywords: proper nouns, project names, tech stacks, library names, the central term of the topic
2. Each keyword is at least {SEARCH_MIN_TOKEN_LEN} characters long
3. Avoid catch-all words such as "memory", "summary", "issue", "fix", "development"
4. Use concrete terms, not descriptive sentences
5. Between 3 and {SUMMARY_KEYWORD_COUNT} keywords

Return JSON: {"keywords": ["keyword1", "keyword2", ...]}
Return a strict JSON object only, not wrapped in a code block.
