---
name: decide_merge
placeholders: [topic, facts, candidates]
json_keys: [action]
---
Decide whether the memory topic below should be merged into an existing knowledge note.

Topic to persist: {topic}
Related facts:
{facts}

Existing note candidates (retrieval hits, in descending relevance):
{candidates}

Criteria:
1. **Merge (action=merge)**: an existing note is highly relevant to the current topic — same tech stack / same project / the continuing evolution of the same matter
2. **Create new (action=new)**: the hit is only tangential or matched a generic word, or this is genuinely a new topic; forcing a merge would pollute the existing note
3. When several notes match, pick the single most relevant `target`; content evolution or conflict can still be merged (say so in `reason`)
4. `target` must be a `rel_path` from the candidate list above — never invent a path

Return JSON: {"action": "merge" or "new", "target": "md path relative to Extracts (when merging)", "reason": "a one-sentence reason"}
Return a strict JSON object only, not wrapped in a code block.
