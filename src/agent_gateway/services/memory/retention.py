"""Quality-weighted retention for the controlled-forgetting pass.

Quality uses three observable signals only — access count, recency, and field
completeness — averaged with equal weight. No model self-rating and no weights
fitted to this store: the buckets are the plan's policy, and the low band is
interpolated instead of pinned to a single tuned number.
"""

from __future__ import annotations

from datetime import datetime

ACCESS_COUNT_CAP = 10
RECENCY_WINDOW_DAYS = 365
COMPLETENESS_FIELDS = ("tags", "episode_id", "type", "context")

RETENTION_HIGH_DAYS = 365
RETENTION_MID_DAYS = 180
RETENTION_LOW_MIN_DAYS = 30
RETENTION_LOW_MAX_DAYS = 90


def field(row: dict, key: str, default=None):
    """Payload fields are flattened by mem0 but nested under metadata in places."""
    if key in row and row[key] not in (None, ""):
        return row[key]
    return (row.get("metadata") or {}).get(key, default)


def _days_ago(value: str, now: datetime) -> int | None:
    if not value:
        return None
    try:
        return max(0, (now - datetime.fromisoformat(str(value))).days)
    except (TypeError, ValueError):
        return None


def quality_score(row: dict, now: datetime | None = None) -> float:
    now = now or datetime.now()
    access = min(int(field(row, "access_count", 0) or 0) / ACCESS_COUNT_CAP, 1.0)
    last = _days_ago(str(field(row, "last_accessed_at", "") or ""), now)
    recency = 0.0 if last is None else max(0.0, 1.0 - last / RECENCY_WINDOW_DAYS)
    present = sum(1 for key in COMPLETENESS_FIELDS if field(row, key))
    completeness = present / len(COMPLETENESS_FIELDS)
    return round((access + recency + completeness) / 3, 3)


def retention_days(quality: float) -> int:
    if quality >= 0.7:
        return RETENTION_HIGH_DAYS
    if quality >= 0.5:
        return RETENTION_MID_DAYS
    span = RETENTION_LOW_MAX_DAYS - RETENTION_LOW_MIN_DAYS
    return int(RETENTION_LOW_MIN_DAYS + span * max(0.0, quality) / 0.5)


def last_touched(row: dict) -> str:
    return str(field(row, "last_accessed_at", "") or field(row, "created_at", "") or "")


def is_expired(row: dict, now: datetime | None = None) -> bool:
    """Expired means untouched for longer than this memory's quality allows."""
    now = now or datetime.now()
    idle = _days_ago(last_touched(row), now)
    if idle is None:
        return False
    return idle >= retention_days(quality_score(row, now))
