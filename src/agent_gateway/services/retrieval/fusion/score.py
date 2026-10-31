"""Score-preserving fusion.

Keeps each arm's own score and interleaves by * score * (base + weight * heat).
Used where scores share a scale (all cosine similarities against one model).
"""

from __future__ import annotations

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
        ordered = sorted(
            merged.values(),
            key=lambda i: heat_key(i, self._heat_max, self._base, self._weight),
            reverse=True,
        )
        return ordered[:limit]


class MaxScoreFusion:
    """Merge arms by plain score, deduplicated by id.

    Used by single-source profiles where every arm already emits one scale.
    """

    name = "max_score"

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


__all__: list[Any] = ["HeatWeightedFusion", "MaxScoreFusion", "heat_key"]
