---
name: gen_new
placeholders: [topic, facts, dir_tree, tops_block, exclude_dirs]
json_keys: [dir, title, content]
---
Turn the memory facts below into a high-quality knowledge note (a new file).

Topic to persist: {topic}
Related facts:
{facts}

Existing note directory structure:
{dir_tree}

Existing top-level directories: {tops_block}

### Requirements

1. **Write strictly from the related facts**: use only the information they contain. Never invent or fabricate non-existent APIs / component names / method names / version numbers / configuration keys / code / data
2. Do not fill in details the facts do not provide — leave them out rather than making them up
3. **Timeline evolution**: when the facts span several days, order them chronologically and keep the date markers on key milestones (e.g. first seen 8/3, root cause confirmed 8/5)
4. **Directory (`dir`)**: you **must prefer reusing** one of the "existing top-level directories" above. Create a new single-level top-level directory only when none of them can hold this topic, and state the reason for creating it. Never nest multiple new directory levels. Never use {exclude_dirs} as the directory
5. **Title (`title`)**: concrete — e.g. "UTSJSONObject null-safe access", not "UTSJSONObject pitfalls"
6. Length 150-500 characters, fact density first, no vague filler
7. The body needs no `#` title line (the script writes it)

Return JSON: {"dir": "relative directory path", "title": "a concrete title", "content": "Markdown body"}
Return a strict JSON object only, not wrapped in a code block.
