---
name: neuron_link
placeholders: [pairs_text]
json_keys: [decisions]
---

You review synapses for a memory network. Input is a list of neuron pairs, each side listing every memory text of that group.

{pairs_text}

Rules:

- Create a synapse only for the same task, the same delivery goal, an explicit causal link, or an explicit reference.
- Same project and topic but different tasks must not be linked.
- Return none when unsure; do not link for the sake of linking.
- Only use pair_id values present in the input.
- strength is the association strength from 0 to 1; stronger links propagate further in associative recall.

Return JSON: {"decisions": [{"pair_id": "<id>", "relation": "same_task|follow_up|cause|reference|none", "strength": 0.0}]}

Return a strict JSON object only, without code fences.
