"""Relevance floors per embedding model.

Floors are measured, not shipped: the auto-calibration task samples the store
when a model is first used and writes `retrieval_thresholds.json` in the data
dir. Until that file exists the provisional values below keep recall answering
instead of failing every call.
"""

from __future__ import annotations

import json
from pathlib import Path

from agent_gateway.core.log_utils import setup_logging
from agent_gateway.core.paths import gateway_home

logger = setup_logging("server")

FLOOR_KEYS = ("hot", "cold", "expanded", "notes_coverage", "notes_semantic")

#: Used only before the first successful calibration of the active model.
PROVISIONAL_FLOORS: dict[str, float] = {
    "hot": 0.55,
    "cold": 0.50,
    "expanded": 0.50,
    "notes_coverage": 0.50,
    "notes_semantic": 0.35,
}

_warned: set[str] = set()


def override_path() -> Path:
    """Calibrated floors live in the data dir, written by auto-calibration."""
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
    if any(entry.get(key) is None for key in FLOOR_KEYS):
        return None
    return {key: float(entry[key]) for key in FLOOR_KEYS}


def is_calibrated(tag: str) -> bool:
    return _override(tag) is not None


def floors_for(tag: str) -> dict[str, float]:
    """Floors for a model; provisional values until calibration lands."""
    tuned = _override(tag)
    if tuned is not None:
        return tuned
    if tag not in _warned:
        _warned.add(tag)
        logger.warning(
            f"[thresholds] no calibrated floors for {tag!r}; using provisional "
            f"values until auto-calibration finishes"
        )
    return dict(PROVISIONAL_FLOORS)
