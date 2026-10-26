---
name: merge_cross_batch
placeholders: [entries]
json_keys: []
---
These are memory groups produced by batched aggregation; topics may repeat across batches. Consolidate groups whose `topic` is the same or similar into one.

Memory groups:
{entries}

Requirements: after merging, `facts` is ordered chronologically (earliest first); when a topic evolved across batches, keep the evolution and the date markers on key milestones.

Return JSON: {"groups": [{"topic": "merged topic", "facts": "complete merged fact text, ordered chronologically", "tags": ["tag"], "retain": true}]}
Return a strict JSON object only, not wrapped in a code block.
