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

    def fuse(self, arms: list[ArmResult], limit: int) -> list[RetrievedItem]:
        merged: dict[str, RetrievedItem] = {}
        for arm in arms:
            for item in arm.items:
                existing = merged.get(item.id)
                if existing is None or item.score > existing.score:
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

    def fuse(self, arms: list[ArmResult], limit: int) -> list[RetrievedItem]:
        merged: dict[str, RetrievedItem] = {}
        for arm in arms:
            for item in arm.items:
                existing = merged.get(item.id)
                if existing is None or item.score > existing.score:
                    merged[item.id] = item
        return sorted(merged.values(), key=lambda i: i.score, reverse=True)[:limit]


class RRFFusion:
    """Rank-only fusion for lists whose scores are not comparable.

    Each input ArmResult is one already-ranked list and contributes
    `1 / (k + rank)` per item, so an item that both passes agree on outranks one
    that only a single pass found near the top. k is the published RRF default
    (Cormack et al., 2009), not a value fitted to any store.
    """

    name = "rrf"
    DEFAULT_K = 60

    def __init__(self, k: int = DEFAULT_K):
        self._k = k

    def fuse(self, arms: list[ArmResult], limit: int) -> list[RetrievedItem]:
        totals: dict[str, float] = {}
        best: dict[str, RetrievedItem] = {}
        for arm in arms:
            for rank, item in enumerate(arm.items):
                totals[item.id] = totals.get(item.id, 0.0) + 1.0 / (self._k + rank + 1)
                current = best.get(item.id)
                if current is None or item.score > current.score:
                    best[item.id] = item
        ordered = sorted(best.values(), key=lambda i: -totals[i.id])[:limit]
        return [replace(i, score=totals[i.id], score_kind="rrf") for i in ordered]


__all__: list[Any] = [
    "HeatWeightedFusion",
    "MaxScoreFusion",
    "RRFFusion",
    "heat_key",
]
