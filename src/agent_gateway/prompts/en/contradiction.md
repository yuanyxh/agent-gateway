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

Also extract the durable semantic layer from the new memory:
- "entities": named entities as {"name", "type", "aliases"}; type is one of person, project, system, file, concept, place, organization
- "facts": subject-predicate-object triples as {"subject", "predicate", "object", "confidence"}, only when the memory states a stable fact

Return JSON: {"relation": "<type>", "reason": "<one-sentence reason>", "conflicting_id": "<id of the conflicting memory, only for contradiction>", "entities": [...], "facts": [...]}
Return a strict JSON object only, not wrapped in a code block.
