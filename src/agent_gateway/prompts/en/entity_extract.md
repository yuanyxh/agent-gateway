---
name: entity_extract
placeholders: [memories_text]
json_keys: [memories]
---

You extract entities and facts from memory records. Each record starts with its [id].

{memories_text}

Extract entities and facts for every record. Use only what the text says; do not infer and do not translate names.

Return JSON:
{"memories": [{"id": "<original id>", "entities": [{"name": "<as written>", "type": "person|place|food|organization|concept|other", "aliases": ["<other spelling>"]}], "facts": [{"subject": "<entity name>", "predicate": "<short verb or relation>", "object": "<entity name or value>", "polarity": "positive|negative|neutral", "confidence": 0.0}]}]}

Rules:
- Every record must appear in the output; use empty arrays when nothing is found
- Prefer the entity names listed for that record in subject and object
- confidence is between 0.0 and 1.0
Return only a strict JSON object, no code block.
