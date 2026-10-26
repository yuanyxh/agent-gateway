---
name: knowledge_expand
placeholders: [theme, done, scope, exclude, disliked]
json_keys: [concept, why_now, exhausted]
---
You are planning a learning path for someone studying "{theme}". They like the principles of **why protocols and systems are designed this way**, not industry solution panoramas, and not glossaries.

Scope of this topic (when empty, use the topic's textbook table of contents):
{scope}

Explicitly out of scope:
{exclude}

Directions the user explicitly disliked (do not pick them, and do not pick their adjacent topics):
{disliked}

Concepts already covered (in order):
{done}

Give the **next concept worth digging into**. It must satisfy all of the following:

- Inside the scope, and a **core concept** in the topic's textbook or standards table of contents
- Stable: not a product name, vendor solution, recent buzzword or industry trend
- Specific to one mechanism: a single card can explain it at the principle level, not a whole domain overview
- It follows naturally from the previous concepts — what would the reader ask next?
- One at a time, never a list
- **`concept` must be the name the field itself uses** (a short, canonical term): imagine looking it
  up in a textbook index or Wikipedia and write exactly what they would write. For networking write
  "TCP/IP four-layer model" rather than "layered protocol stack model"; for compilers write
  "intermediate representation", "lexical analysis", "symbol table".
  **Never invent a descriptive phrasing of your own** — it is used verbatim to search authoritative
  sources, and an invented term matches nothing, so the card can only be fabricated. It is also the
  card title, and an invented term makes an ugly one. Put "why learn it now" in `why_now`

If any of the above fails, do not pad the path with an adjacent topic — return exhausted instead.
When the core concepts inside the scope are done, return exhausted:

{"concept": "", "why_now": "", "exhausted": true}

Output JSON only:

{"concept": "concept name", "why_now": "one line (40 words max): why learn it now", "exhausted": false}
