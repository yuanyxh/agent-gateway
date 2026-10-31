"""Relevance floors per embedding model.

Floors are model-specific: swapping the embedder changes the score distribution,
so an uncalibrated model must fail loudly instead of silently reusing old values.
"""

from __future__ import annotations

import json
from pathlib import Path

from agent_gateway.core.log_utils import setup_logging
from agent_gateway.core.paths import gateway_home

logger = setup_logging("server")

FLOOR_KEYS = ("hot", "cold", "expanded", "search", "notes_coverage", "notes_semantic")

FLOORS: dict[str, dict[str, float | None]] = {
    "minilm384": {
        "hot": 0.68,
        "cold": 0.56,
        "expanded": 0.56,
        "search": 0.35,
        "notes_coverage": 0.60,
        "notes_semantic": 0.40,
    },
    # Measured on 249 annotated queries: these keep 96.4% of queries non-empty
    # while raising precision@5 from 0.397 (minilm) to 0.662.
    "jinazh768": {
        "hot": 0.62,
        "cold": 0.55,
        "expanded": 0.55,
        "search": 0.35,
        "notes_coverage": 0.50,
        "notes_semantic": 0.35,
    },
}


#: Tags whose floors are copied from another model and need real measurements.
SEEDED_TAGS: set[str] = set()
_warned: set[str] = set()


class UncalibratedThresholds(RuntimeError):
    """Raised when a model has no calibrated floors yet."""


def override_path() -> Path:
    """Calibrated floors live in the data dir so tuning needs no code change."""
    return gateway_home() / "retrieval_thresholds.json"


def _override(tag: str) -> dict[str, float] | None:
    path = override_path()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    entry = data.get(tag)
    if not isinstance(entry, dict):
        return None
    if tag in SEEDED_TAGS and tag not in _warned:
        _warned.add(tag)
        logger.warning(
            f"[thresholds] {tag} still uses seeded floors; run "
            f"scripts/calibrate_thresholds.py to measure them"
        )
    missing = [key for key in FLOOR_KEYS if entry.get(key) is None]
    if missing:
        return None
    return {key: float(entry[key]) for key in FLOOR_KEYS if key in entry}


def floors_for(tag: str) -> dict[str, float]:
    tuned = _override(tag)
    if tuned is not None:
        return tuned
    entry = FLOORS.get(tag)
    if entry is None:
        raise KeyError(f"No threshold table for model tag {tag!r}")
    missing = [key for key in FLOOR_KEYS if entry.get(key) is None]
    if missing:
        raise UncalibratedThresholds(
            f"Thresholds for model tag {tag!r} are not calibrated: {', '.join(missing)}. "
            f"Run scripts/calibrate_thresholds.py before enabling this model."
        )
    return {key: float(entry[key]) for key in FLOOR_KEYS if entry[key] is not None}
