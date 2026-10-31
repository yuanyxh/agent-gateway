"""Measure relevance floors from the store itself.

Hand a record its own opening words as the query. If the store cannot return
that record, the floor is too high; the low percentile of those self-scores is
a floor that needs no labelled queries and no knowledge of the model, so a new
embedder can calibrate itself on first use.
"""

from __future__ import annotations

from typing import Callable, Iterable

#: Characters taken from the record as its probe query.
PROBE_CHARS = 20
#: Median of the self-scores. Measured against two hand-calibrated tables: the
#: median lands within 0.02 of both (hot 0.616 vs 0.62, cold 0.569 vs 0.55),
#: while the 5th percentile sits ~0.2 lower and admits unrelated records.
QUANTILE = 0.50
#: Fewer probes than this and the percentile is noise.
MIN_SAMPLES = 20

Search = Callable[[str], list[tuple[str, float]]]


def percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[index]


def self_scores(samples: Iterable[tuple[str, str]], search: Search) -> list[float]:
    """Score each record earns when queried by its own opening words.

    A record absent from its own result list contributes nothing: that is the
    miss the percentile is meant to measure.
    """
    scores: list[float] = []
    for record_id, text in samples:
        probe = (text or "").strip()[:PROBE_CHARS]
        if len(probe) < 4:
            continue
        for found_id, score in search(probe):
            if str(found_id) == str(record_id):
                scores.append(float(score))
                break
    return scores


def floor_from_scores(scores: list[float], quantile: float = QUANTILE) -> float | None:
    """Representative self-score, or None when there are too few probes."""
    if len(scores) < MIN_SAMPLES:
        return None
    return round(percentile(scores, quantile), 3)


__all__ = [
    "MIN_SAMPLES",
    "PROBE_CHARS",
    "QUANTILE",
    "floor_from_scores",
    "percentile",
    "self_scores",
]
