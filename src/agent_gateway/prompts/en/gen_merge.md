---
name: gen_merge
placeholders: [topic, facts, target, context]
json_keys: [action]
---
Add the memory facts below to an existing knowledge note (appending new information).

Topic to persist: {topic}
Related facts:
{facts}

Target note: {target}
Relevant passages of the target note (selected by relevance to the facts, not the whole file):
{context}

### Requirements

1. **Write strictly from the related facts**: use only the information they contain, never invent
2. **Write only what the existing content does not already cover**: do not repeat details already written
3. **Timeline evolution**: when this continues the existing content over time, mark the dates (e.g. first seen 8/3, root cause confirmed 8/5) so "A first, later changed to B" is visible
4. **On conflict**: the facts win — note the difference from the old conclusion
5. Length 150-500 characters, fact density first
6. The body needs no `#` title line (the script writes it)
7. **When correcting an old conclusion**: set "supersedes" to true if this content negates the old section's conclusion; set false when only adding information
8. **No-increment check (important)**: first compare the "related facts" with the "relevant passages of the target note". If every point of the facts is already covered and there is no substantial new information → return {"action": "no_increment", "reason": "briefly list what already exists"}. Return normally only when uncovered new information exists; when unsure, treat it as having an increment (keep rather than lose)

Return JSON: {"action": "merge", "title": "section title", "content": "Markdown body", "supersedes": true or false}
Or, when there is no increment: {"action": "no_increment", "reason": "brief list of what duplicated"}
Return a strict JSON object only, not wrapped in a code block.
