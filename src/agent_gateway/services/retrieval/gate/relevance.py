"""Relevance gates.

`LayerPresenceGate` decides ok/weak/no from where a hit came from: only a
semantic-layer hit above its own floor counts as a real answer.
"""

from __future__ import annotations

from agent_gateway.services.retrieval.types import GateVerdict, Query, RetrievedItem

DEFAULT_SEMANTIC_LAYERS = ("hot", "cold")


class LayerPresenceGate:
    """ok when a semantic-layer hit survives the arm's own absolute floor.

    ``semantic_layers`` is per-profile: a notes-only pipeline carries its own
    layer name, and hard-coding hot/cold there would report every hit as weak.
    """

    name = "layer_presence"

    def __init__(self, semantic_layers: tuple[str, ...] = DEFAULT_SEMANTIC_LAYERS):
        self._semantic_layers = tuple(semantic_layers)

    def judge(
        self,
        query: Query,
        items: list[RetrievedItem],
        related: list[RetrievedItem] | None = None,
    ) -> GateVerdict:
        related = related or []
        has_semantic = any(i.layer in self._semantic_layers for i in items)
        if has_semantic:
            return GateVerdict(status="ok", reason="semantic_layer_hit")
        if items or related:
            return GateVerdict(status="weak_match", reason="lexical_or_related_only")
        return GateVerdict(status="no_match", reason="empty")


class PassThroughGate:
    """Report presence only, for deterministic lookups with no score floor.

    Symbol lookup returns exact/near-exact matches, so a low score is not
    evidence of a weak answer; the caller's own status machine decides.
    """

    name = "pass_through"

    def judge(
        self,
        query: Query,
        items: list[RetrievedItem],
        related: list[RetrievedItem] | None = None,
    ) -> GateVerdict:
        if items or related:
            return GateVerdict(status="ok", reason="results_present")
        return GateVerdict(status="no_match", reason="empty")
