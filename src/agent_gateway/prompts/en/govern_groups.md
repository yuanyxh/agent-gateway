---
name: govern_groups
placeholders: [candidates_text]
json_keys: [decisions]
---

You review memory episode groups. Input contains multi-member groups or pairs of single memories.

{candidates_text}

Rules:

- Keep memories in one group only when they belong to the same task or delivery goal.
- Use the session / task / file fields first: the same session or the same task means the same task.
- Do not merge memories that share a project or topic when both session and task differ and the text shows no shared delivery goal.
- Return keep only when the evidence is insufficient to decide.
- Use only the ids and candidate_ids that appear in the input.
- For split, groups must cover every member exactly once and must not overlap.
- For a multi-member group, merge_with must be another candidate_id in the same batch.
- For a pair, merge means the two memories belong in one group; merge_with is not needed.
- For a group_pair, only keep or merge are allowed; merge means the two groups should become one, with no merge_with.

Return JSON: {"decisions": [{"candidate_id": "<id>", "action": "keep|split|merge", "groups": [["<member id>"]], "merge_with": "<candidate id>", "reason": "<at most 50 chars>"}]}

Return a strict JSON object only, without code fences.
