---
name: aggregate
placeholders: [label, facts_text]
json_keys: []
---
You are preprocessing a batch of memory facts for aggregation: group them by topic, merge and de-duplicate within each group, and drop one-off information with no long-term value.

Memory facts{label}:
{facts_text}

### Requirements

1. **Group by topic**: facts that are semantically close or about the same subject belong in one group
2. **Merge and de-duplicate within a group**: repeated or similar records of the same thing become a single coherent text; do not list them over and over
3. **Timeline evolution**: for a topic spanning several days, order the facts chronologically inside `facts` (earliest first) so the trajectory "A first, later changed to B" is visible; when the content changed, keep the evolution instead of only the final state
4. **Date the key milestones**: mark dates in `facts` for root-cause identification, option selection, decisions, and fix completion — e.g. (first seen 8/3, root cause confirmed 8/5)
5. **Assess retention value (`retain`)**: decide for each group whether it deserves to become a long-term knowledge note. Prefer omission over inclusion —
   - **retain=true**: carries knowledge reusable over the long term, such as technical approaches, architectural decisions, pitfalls and their solutions, reusable conclusions
   - **retain=false**: always false for pure temporary work records (task-number logs, single investigation traces without detail, process fragments like "looked into X", "discussed Y"), vague conclusions with no concrete approach, fuzzy experience with no locators (file paths / component names / parameter names / decision conditions / error messages), and intermediate states that mean nothing a few days later
   - **Prefer missing a note to producing a temporary record**: when unsure, mark false
6. **Drop one-off information**: temporary states, outdated configuration, pure log fragments, and context-free sentences enter no group
7. **Topics must be concrete**: e.g. "Bluetooth printer SDK integration", not "printing"
8. **Use only information present in the memories**: never invent or fabricate non-existent APIs / component names / method names / version numbers / configuration keys / code / data
9. **Completeness of `facts`**: it must carry every reusable detail from all memory entries of that topic — especially file paths, component and interface names, parameter names, decision conditions, and error messages. Do not drop them because of grouping

Return JSON: {"groups": [{"topic": "a concrete topic", "facts": "facts merged, de-duplicated and ordered chronologically", "tags": ["tag"], "retain": true}]}
Return {"groups": []} when there is no valid content.
Return a strict JSON object only, not wrapped in a code block.
