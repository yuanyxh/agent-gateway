---
name: extract_rules
placeholders: [facts_text, level_labels, titles_block]
json_keys: [candidates, norms]
---
You are extracting norms from a batch of already-aggregated long-term memories. That aggregated content mixes two kinds: "an occasional bug / a one-off fix" and "a genuine norm / rule". This pass extracts **only the latter**, preferring omission over inclusion.

Aggregated memories of this period:
{facts_text}

### What counts as a genuine norm (extractable) — four criteria, the more the better
1. **Proactive convention**: a code of conduct or agreement set in advance, not a remedy applied after stepping in a pit
2. **Abstract and general**: it still holds after switching project or tech stack; the sentence still makes sense once the concrete library names / field names / version numbers / error messages are removed
3. **Recurring**: the same lesson has been recorded repeatedly across memories and scenarios, rather than met once by accident
4. **Architecture / principle**: architectural decisions, design principles, engineering process agreements (e.g. layered decoupling, single responsibility, configuration as constants, verify before committing)

### What does not count as a norm (never extract) — discard all of these
- Conclusions drawn from a specific bug fix (e.g. "this field must be tested for both IS NULL and the empty string", "this tool's parameters require probing the schema first"), even when they can be stated in one sentence
- One-off remedies tied to a specific error message / version number / environment
- A single pitfall experience that did not recur across scenarios
- Pure step-by-step operational records

### How to tell
- Ask yourself: had I never hit that pit, would I still state this rule on my own? — No → it is a pitfall lesson, not a norm
- Ask yourself: with every technical term removed, what is left of this sentence? — Nothing left → not abstract enough, discard

### Extraction requirements
1. Extract 0-5 norms, preferring omission; return an empty array when nothing qualifies
2. Each norm states one thing clearly and actionably, **bound to no concrete technical detail**
3. Each norm carries `keywords`: 3-5 trigger keywords / scenarios (covering the tech domain, the action, the subject, and other trigger surfaces), so the tool description can generate a "triggered when xxx" signal
4. Choose `level` from the list below; use `global` when it cannot be decided:
{level_labels}

### Avoid duplication
Existing titles (new norm titles must not duplicate or heavily overlap them):
{titles_block}

Return JSON: {"candidates": ["list of memory topics judged to be norms"], "norms": [{"title": "title", "level": "global", "summary": "an actionable norm", "keywords": ["trigger1", "trigger2", "trigger3"], "source": "source memory topic"}]}
Return {"candidates": [], "norms": []} when there is nothing norm-like.
Return a strict JSON object only, not wrapped in a code block.
