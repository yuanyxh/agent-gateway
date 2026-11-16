---
name: knowledge_card
placeholders: [theme, concept, evidence, scope, exclude]
json_keys: [card]
---
You are writing a knowledge card for someone who likes the principles of **why protocols and systems are designed this way**. They do not need a glossary — they need the "why".

Theme: {theme}
Concept: {concept}
Topic scope: {scope}
Explicitly out of scope (never write about these): {exclude}

Source points (**rely only on these**; do not add numbers, versions or facts that are not in them):

{evidence}

How to write it:

- Write only about this concept's mechanism and principle: do not expand into a domain overview, and do not cover products, vendor solutions or industry trends
- **Check the source material belongs to the topic's domain first**: the same term can mean different things in different fields. If the material covers a namesake concept from another domain and has nothing to do with the theme or scope, do not force a card — output {"card": ""}
- Use Markdown: headings, **bold** for key judgements, lists for parallel items — do not cram everything into one block
- Open with a concrete dilemma, not with "X is ..."
- Cover three things: why you cannot avoid it, what it actually buys you, what it costs
- **800–1500 words** — every paragraph must carry information, no filler; wrap up at 1500
- End with a hook that follows naturally from the material above. **Do not introduce new concepts, terms or acronyms that are not in the material**
- No filler words like "firstly / secondly / in summary", no "this article" or "the material shows"

Output JSON only:

{"card": "Markdown body"}
