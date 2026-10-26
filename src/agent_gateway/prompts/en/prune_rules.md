---
name: prune_rules
placeholders: [level, limit, items]
json_keys: [keep_indexes]
---
The "{level}" rules file exceeds its {limit}-entry limit; the least reusable entries must be pruned.

Current entries:
{items}

Requirement: keep the {limit} most reusable, most actionable entries and drop outdated, low-reuse, or duplicate ones.

Return JSON: {"keep_indexes": [array of kept entry indexes]}
Return only a strict JSON object, without code fences.
