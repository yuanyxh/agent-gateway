---
name: replay_sequence
placeholders: [current, sequences_text]
json_keys: [reinforce]
---

You replay memory sequences. Current memory:

{current}

Below are association paths that start from it. Each line is one path; `id|title` is a node and the arrow is the jump direction:

{sequences_text}

Decide which paths are real associations: semantically coherent and sensible. Drop pairs that only share vector similarity.

Return JSON: {"reinforce": [["<neuron id>", "<neuron id>", "..."]]}
Return only a strict JSON object, no code block.
