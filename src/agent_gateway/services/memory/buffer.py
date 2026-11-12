"""Working-memory buffer: recent activations with a short half-life."""

from __future__ import annotations

from datetime import datetime, timedelta

from agent_gateway.services.memory import store

TTL_MINUTES = 30
DECAY_PER_5MIN = 0.9
MAX_ROWS = 200
KEEP = 0.01


def _parse(value) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def decayed(activation: float, updated_at, now: datetime) -> float:
    parsed = _parse(updated_at)
    if parsed is None:
        return 0.0
    age = (now - parsed).total_seconds() / 60
    if age < 0 or age > TTL_MINUTES:
        return 0.0
    return max(0.0, float(activation) * (DECAY_PER_5MIN ** (age / 5)))


def writeback(
    items: list[dict], *, now: datetime | None = None, db_path: str | None = None
) -> int:
    now = now or datetime.now()
    written = 0
    with store.connect(db_path) as connection:
        for item in items:
            neuron_id = str(item.get("neuron_id") or "")
            activation = float(item.get("activation") or 0.0)
            if not neuron_id or activation <= 0:
                continue
            connection.execute(
                "INSERT INTO activation_buffer (neuron_id, activation, updated_at) "
                "VALUES (?, ?, ?) ON CONFLICT(neuron_id) DO UPDATE SET "
                "activation=MAX(activation_buffer.activation, excluded.activation), "
                "updated_at=excluded.updated_at",
                (neuron_id, min(1.0, activation), now.isoformat()),
            )
            written += 1
        _trim(connection, now)
    return written


def _trim(connection, now: datetime) -> int:
    cutoff = (now - timedelta(minutes=TTL_MINUTES)).isoformat()
    removed = connection.execute(
        "DELETE FROM activation_buffer WHERE updated_at < ?", (cutoff,)
    ).rowcount
    total = connection.execute("SELECT COUNT(*) FROM activation_buffer").fetchone()[0]
    if total > MAX_ROWS:
        removed += connection.execute(
            "DELETE FROM activation_buffer WHERE neuron_id IN ("
            "SELECT neuron_id FROM activation_buffer "
            "ORDER BY activation ASC, updated_at ASC LIMIT ?)",
            (total - MAX_ROWS,),
        ).rowcount
    return removed


def read(
    *, now: datetime | None = None, limit: int = MAX_ROWS, db_path: str | None = None
) -> dict[str, float]:
    now = now or datetime.now()
    with store.connect(db_path) as connection:
        rows = connection.execute(
            "SELECT neuron_id, activation, updated_at FROM activation_buffer"
        ).fetchall()
    out = {}
    for neuron_id, activation, updated_at in rows:
        value = decayed(activation, updated_at, now)
        if value > KEEP:
            out[str(neuron_id)] = round(value, 4)
    ordered = sorted(out.items(), key=lambda item: -item[1])[:limit]
    return dict(ordered)
