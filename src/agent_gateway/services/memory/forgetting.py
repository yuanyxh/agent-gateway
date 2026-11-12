"""FSRS-style forgetting: retrievability, stability growth and per-row decay."""

from __future__ import annotations

import math
from datetime import datetime

from agent_gateway.services.memory import network, store

DECAY_BASE = 0.9
GAIN = 0.8
MIN_STABILITY = 1.0
MAX_STABILITY = 3650.0
DEFAULT_STABILITY = 30.0
DEFAULT_DIFFICULTY = 5.0


def retrievability(stability: float, elapsed_days: float) -> float:
    stability = max(MIN_STABILITY, float(stability or DEFAULT_STABILITY))
    elapsed = max(0.0, float(elapsed_days))
    return math.exp(math.log(DECAY_BASE) * elapsed / stability)


def next_stability(
    stability: float,
    difficulty: float,
    retrievable: float,
    *,
    gain: float = GAIN,
) -> float:
    base = max(MIN_STABILITY, float(stability or DEFAULT_STABILITY))
    ease = 1.0 - min(1.0, max(0.0, float(difficulty or DEFAULT_DIFFICULTY)) / 10.0)
    grown = base * (1.0 + gain * (1.0 - retrievable) * (0.5 + 0.5 * ease))
    return min(MAX_STABILITY, max(MIN_STABILITY, grown))


def initial_stability(importance: int) -> float:
    return min(MAX_STABILITY, 3.0 + 12.0 * max(0, min(10, int(importance))) / 10.0)


def _parse(value) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def days_between(start, now: datetime) -> float:
    parsed = _parse(start)
    if parsed is None:
        return 0.0
    return max(0.0, (now - parsed).total_seconds() / 86400)


def apply_decay_v3(*, now: datetime | None = None, db_path: str | None = None) -> dict:
    now = now or datetime.now()
    last = network.get_meta("last_decay_at", db_path=db_path)
    if not last:
        network.set_meta("last_decay_at", now.isoformat(), db_path=db_path)
        return {"rows": 0, "days": 0.0}
    days = days_between(last, now)
    if days <= 0:
        return {"rows": 0, "days": 0.0}
    with store.connect(db_path) as connection:
        rows = connection.execute(
            "SELECT node_id, strength, stability FROM memory_activation"
        ).fetchall()
        updates = []
        for node_id, strength, stability in rows:
            factor = retrievability(stability or DEFAULT_STABILITY, days)
            updates.append((max(0.0, float(strength) * factor), node_id))
        if updates:
            connection.executemany(
                "UPDATE memory_activation SET strength=? WHERE node_id=?", updates
            )
    network.set_meta("last_decay_at", now.isoformat(), db_path=db_path)
    return {"rows": len(updates), "days": days}
