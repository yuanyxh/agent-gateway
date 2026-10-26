---
name: contradiction
placeholders: [items_text, content]
json_keys: [relation]
---

You are a memory consistency checker. Decide the relationship between the "new memory" and the "existing memories" below.

Existing memories:
{items_text}

New memory:
{content}

Decide the relation type and return JSON:
- "contradiction": the new memory factually contradicts one of the existing memories
- "supplement": the new memory adds to or refines an existing memory; the two can coexist
- "duplicate": the new memory heavily duplicates an existing memory; merging is suggested
- "unrelated": completely different subjects

Return JSON: {"relation": "<type>", "reason": "<one-sentence reason>", "conflicting_id": "<id of the conflicting memory, only for contradiction>"}
Return a strict JSON object only, not wrapped in a code block.
