"""Measure relevance floors from the store itself.

Hand a record its **full text** to the retriever as the query. If the store
cannot return that record, the floor is too high; a low percentile of those
self-scores is a floor that needs no labelled queries and no knowledge of the
model, so a new embedder can calibrate itself on first use.

The probe must be the whole record: the opening slice is a different query with
a different score distribution, and a floor measured on it is not the floor the
retriever applies to real queries.
"""

from __future__ import annotations

from typing import Callable, Iterable

#: Median of the self-scores. The quantile is re-measured from full-text probes;
#: the old tables derived from 20-character prefixes are void.
QUANTILE = 0.50
#: Fewer probes than this and the percentile is noise.
MIN_SAMPLES = 20
#: A probe shorter than this carries no retrievable signal.
PROBE_MIN_CHARS = 4

Search = Callable[[str], list[tuple[str, float]]]


def percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[index]


def self_scores(
    samples: Iterable[tuple[str, str]],
    search: Search,
    stats: dict | None = None,
) -> list[float]:
    """Score each record earns when queried by its entire text.

    A record absent from its own result list contributes nothing: that is the
    miss the percentile is meant to measure. `stats` is filled in place with
    probe counts so the caller can report coverage.
    """
    scores: list[float] = []
    for record_id, text in samples:
        probe = (text or "").strip()
        if len(probe) < PROBE_MIN_CHARS:
            if stats is not None:
                stats["too_short"] = stats.get("too_short", 0) + 1
            continue
        if stats is not None:
            stats["probes"] = stats.get("probes", 0) + 1
            stats["chars_total"] = stats.get("chars_total", 0) + len(probe)
            stats["max_chars"] = max(stats.get("max_chars", 0), len(probe))
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
    "PROBE_MIN_CHARS",
    "QUANTILE",
    "floor_from_scores",
    "percentile",
    "self_scores",
]
