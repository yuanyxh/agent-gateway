---
name: knowledge_gate
placeholders: [theme, scope, exclude, concept, why_now, disliked]
json_keys: [accept, reason]
---
You are reviewing whether a knowledge card's subject is out of bounds. Judge only; do not write the card.

Theme: {theme}
Scope: {scope}
Explicitly out of scope: {exclude}
Directions the user explicitly disliked: {disliked}
Candidate concept: {concept}
Why now: {why_now}

All five must hold:

1. It is inside the scope
2. It is a stable concept in the field's textbooks or standards — not a product name, vendor solution, recent buzzword or industry trend
3. It is specific to one mechanism: a single card can explain it at the principle level, not a whole domain overview
4. It is a core concept of the topic, not an adjacent topic chosen just to keep the chain going
5. It is not an extension of a direction the user explicitly disliked (this holds automatically when the list is empty)

If any one fails, set accept=false and state in `reason` which one it violates (30 words max).
Output JSON only:

{"accept": true, "reason": ""}
