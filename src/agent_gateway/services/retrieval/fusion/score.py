"""Score-preserving fusion.

Keeps each arm's own score and interleaves by * score * (base + weight * heat).
Used where scores share a scale (all cosine similarities against one model).
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from agent_gateway.services.retrieval.types import ArmResult, RetrievedItem


def heat_key(item: RetrievedItem, heat_max: float, base: float, weight: float) -> float:
    heat = item.extra.get("heat_score", 0.5)
    if not isinstance(heat, (int, float)):
        heat = 0.5
    return float(item.score) * (base + weight * min(float(heat), heat_max))


class HeatWeightedFusion:
    """Merge arms preserving scores, ranked by score times a heat multiplier."""

    name = "heat_weighted"

    def __init__(self, heat_max: float = 1.0, base: float = 0.8, weight: float = 0.2):
        self._heat_max = heat_max
        self._base = base
        self._weight = weight

    def rank_key(self, item: RetrievedItem) -> float:
        """Ordering key shared with post-fusion boosts, so a re-sort keeps it."""
        return heat_key(item, self._heat_max, self._base, self._weight)

    def fuse(
        self,
        arms: list[ArmResult],
        limit: int,
        weights: dict[str, float] | None = None,
    ) -> list[RetrievedItem]:
        merged: dict[str, RetrievedItem] = {}
        for arm in arms:
            factor = (weights or {}).get(arm.name, 1.0)
            for item in arm.items:
                existing = merged.get(item.id)
                if existing is None or item.score * factor > existing.score:
                    merged[item.id] = item
        ordered = sorted(merged.values(), key=self.rank_key, reverse=True)
        return ordered[:limit]


class MaxScoreFusion:
    """Merge arms by plain score, deduplicated by id.

    Used by single-source profiles where every arm already emits one scale.
    """

    name = "max_score"

    def rank_key(self, item: RetrievedItem) -> float:
        return float(item.score)

    def fuse(
        self,
        arms: list[ArmResult],
        limit: int,
        weights: dict[str, float] | None = None,
    ) -> list[RetrievedItem]:
        merged: dict[str, RetrievedItem] = {}
        for arm in arms:
            factor = (weights or {}).get(arm.name, 1.0)
            for item in arm.items:
                existing = merged.get(item.id)
                if existing is None or item.score * factor > existing.score:
                    merged[item.id] = item
        return sorted(merged.values(), key=lambda i: i.score, reverse=True)[:limit]


class SumScoreFusion:
    """Add up per-sub-query scores for the same id.

    Used where each sub-query is an independent claim (one per keyword): a
    record that answers three keywords outranks one that answers a single
    keyword more closely, which a max-score merge cannot express. The count of
    contributing sub-queries rides along as `keyword_support`.
    """

    name = "sum_score"

    def rank_key(self, item: RetrievedItem) -> float:
        return float(item.score)

    def fuse(
        self,
        arms: list[ArmResult],
        limit: int,
        weights: dict[str, float] | None = None,
    ) -> list[RetrievedItem]:
        merged: dict[str, RetrievedItem] = {}
        for arm in arms:
            factor = (weights or {}).get(arm.name, 1.0)
            for item in arm.items:
                scored = float(item.score) * factor
                existing = merged.get(item.id)
                if existing is None:
                    clone = replace(item, score=scored)
                    clone.extra = dict(item.extra)
                    clone.extra["keyword_support"] = 1
                    merged[item.id] = clone
                else:
                    existing.score += scored
                    existing.extra["keyword_support"] = (
                        int(existing.extra.get("keyword_support", 1)) + 1
                    )
        return sorted(merged.values(), key=self.rank_key, reverse=True)[:limit]


__all__: list[Any] = [
    "HeatWeightedFusion",
    "MaxScoreFusion",
    "SumScoreFusion",
    "heat_key",
]
