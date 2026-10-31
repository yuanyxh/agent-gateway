"""Relevance gates.

`LayerPresenceGate` reproduces the pre-refactor recall semantics and exists so the
pipeline migration stays behaviour-identical. `RelativeRelevanceGate` adds a
model-agnostic margin rule and is enabled per profile once calibration passes.
"""

from __future__ import annotations

from statistics import median

from agent_gateway.services.retrieval.text.analyzer import split_terms
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


class RelativeRelevanceGate:
    """Absolute floor plus relative margin; a lexical anchor always passes.

    Why a relative rule: a fixed floor tuned on one model silently admits weak
    matches on another. `top1 - median` stays comparable across models.
    """

    name = "relative"

    def __init__(
        self,
        floor: float,
        min_margin: float = 0.06,
        probe: int = 5,
        semantic_layers: tuple[str, ...] = DEFAULT_SEMANTIC_LAYERS,
    ):
        self._floor = floor
        self._min_margin = min_margin
        self._probe = probe
        self._semantic_layers = tuple(semantic_layers)

    def _anchor(self, query: Query, items: list[RetrievedItem]) -> bool:
        terms = split_terms(query.text)
        if not terms:
            return False
        for item in items[: self._probe]:
            haystack = (item.content or "").lower()
            if haystack and any(term in haystack for term in terms):
                return True
        return False

    def judge(
        self,
        query: Query,
        items: list[RetrievedItem],
        related: list[RetrievedItem] | None = None,
    ) -> GateVerdict:
        related = related or []
        # Lexical (notes) hits can never reach `ok`: only semantic layers carry
        # similarity scores that the floor and margin mean anything for.
        semantic = [i for i in items if i.layer in self._semantic_layers]
        probe = semantic[: self._probe]
        scores = sorted((float(i.score) for i in probe), reverse=True)
        top1 = scores[0] if scores else 0.0
        med = median(scores) if scores else 0.0
        margin = top1 - med
        anchor = self._anchor(query, probe)
        verdict = GateVerdict(
            top1=round(top1, 4),
            median=round(med, 4),
            margin=round(margin, 4),
            lexical_anchor=anchor,
        )
        if semantic and (
            anchor or (top1 >= self._floor and margin >= self._min_margin)
        ):
            verdict.status = "ok"
            verdict.reason = "lexical_anchor" if anchor else "floor_and_margin"
            return verdict
        if items or related:
            verdict.status = "weak_match"
            verdict.reason = (
                "below_floor" if top1 < self._floor else "insufficient_margin"
            )
            return verdict
        verdict.status = "no_match"
        verdict.reason = "empty"
        return verdict
