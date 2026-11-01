---
name: consolidate
placeholders: [facts_text]
json_keys: [summary]
---

You are a memory consolidation pass. The facts below belong to one topic cluster.

{facts_text}

Merge them into one fact that keeps every distinct detail and every unresolved
conflict. Do not invent facts, do not add advice, and do not repeat a fact twice.

Return JSON: {"summary": "<one fact>", "context": "<when it applies and why this cluster matters>"}
Return a strict JSON object only, not wrapped in a code block.
