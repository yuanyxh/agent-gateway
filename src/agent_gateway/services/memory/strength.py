"""Dynamic strength for memories and synapses: decay, reinforcement and unbinding."""

from __future__ import annotations

import itertools
import math
from datetime import datetime, timedelta

from agent_gateway.core import config
from agent_gateway.core.log_utils import setup_logging
from agent_gateway.services.memory import forgetting, network, store

logger = setup_logging("server")

STRENGTH_HALF_LIFE_DAYS = 30
SYNAPSE_HALF_LIFE_DAYS = 90
ACCESS_REINFORCE = 0.15
PATH_REINFORCE = 0.10
COACTIVATE_REINFORCE = 0.20
UNBIND_WEIGHT = 0.15
UNBIND_DAYS = 60


def _iso(now: datetime | str | None = None) -> str:
    if isinstance(now, datetime):
        return now.isoformat()
    return str(now or datetime.now().isoformat())


def _parse(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _days_between(start: str, now: datetime) -> float:
    parsed = _parse(start)
    if parsed is None:
        return 0.0
    return max(
        0.0,
        (now.replace(tzinfo=None) - parsed.replace(tzinfo=None)).total_seconds()
        / 86400,
    )


def _bump(current: float, delta: float) -> float:
    return min(1.0, current + delta * (1.0 - current))


def _decay_synapses(days: float, db_path: str | None) -> int:
    factor = math.exp(-math.log(2) * days / SYNAPSE_HALF_LIFE_DAYS)
    with store.connect(db_path) as connection:
        return connection.execute(
            "UPDATE synapses SET weight=MAX(0.0, weight*?) WHERE status='active'",
            (factor,),
        ).rowcount


def apply_decay(
    *,
    now: datetime | None = None,
    db_path: str | None = None,
) -> dict:
    now = now or datetime.now()
    last = network.get_meta("last_decay_at", db_path=db_path)
    if not last:
        network.set_meta("last_decay_at", now.isoformat(), db_path=db_path)
        return {"memory_rows": 0, "synapse_rows": 0, "days": 0.0}
    days = _days_between(last, now)
    if days <= 0:
        return {"memory_rows": 0, "synapse_rows": 0, "days": 0.0}
    if config.memory_brain_forgetting():
        result = forgetting.apply_decay_v3(now=now, db_path=db_path)
        return {
            "memory_rows": result["rows"],
            "synapse_rows": _decay_synapses(days, db_path),
            "days": result["days"],
        }
    memory_factor = math.exp(-math.log(2) * days / STRENGTH_HALF_LIFE_DAYS)
    with store.connect(db_path) as connection:
        memory_rows = connection.execute(
            "UPDATE memory_activation SET strength=MAX(0.0, strength*?)",
            (memory_factor,),
        ).rowcount
    synapse_rows = _decay_synapses(days, db_path)
    network.set_meta("last_decay_at", now.isoformat(), db_path=db_path)
    return {"memory_rows": memory_rows, "synapse_rows": synapse_rows, "days": days}


def reinforce_memory(
    node_id: str,
    *,
    delta: float = ACCESS_REINFORCE,
    now: datetime | None = None,
    db_path: str | None = None,
) -> float:
    now = now or datetime.now()
    with store.connect(db_path) as connection:
        previous = connection.execute(
            "SELECT stability, difficulty, last_activated_at FROM memory_activation "
            "WHERE node_id=?",
            (node_id,),
        ).fetchone()
        row = connection.execute(
            """
            INSERT INTO memory_activation
              (node_id, strength, tier, activation_count, last_activated_at)
            VALUES (?, ?, 'active', 1, ?)
            ON CONFLICT(node_id) DO UPDATE SET
              strength=MIN(1.0, memory_activation.strength
                           + ? * (1.0 - memory_activation.strength)),
              activation_count=memory_activation.activation_count + 1,
              last_activated_at=excluded.last_activated_at
            RETURNING strength
            """,
            (node_id, delta, now.isoformat(), delta),
        ).fetchone()
        if config.memory_brain_forgetting():
            stability = float(previous[0] or 0.0) if previous else 0.0
            if stability <= 0:
                stability = forgetting.initial_stability(5)
            difficulty = float(previous[1] or 0.0) if previous else 0.0
            last_at = previous[2] if previous else now.isoformat()
            elapsed = forgetting.days_between(last_at, now)
            retrievable = forgetting.retrievability(stability, elapsed)
            grown = forgetting.next_stability(
                stability, difficulty or forgetting.DEFAULT_DIFFICULTY, retrievable
            )
            connection.execute(
                "UPDATE memory_activation SET stability=? WHERE node_id=?",
                (grown, node_id),
            )
    return float(row[0]) if row else 0.0


def reinforce_synapse(
    synapse_id: int,
    *,
    delta: float = PATH_REINFORCE,
    now: datetime | None = None,
    db_path: str | None = None,
) -> float:
    now = now or datetime.now()
    with store.connect(db_path) as connection:
        row = connection.execute(
            "SELECT weight, activation_count FROM synapses WHERE synapse_id=?",
            (synapse_id,),
        ).fetchone()
        if row is None:
            return 0.0
        weight = _bump(float(row[0]), delta)
        connection.execute(
            "UPDATE synapses SET weight=?, activation_count=?, last_activated_at=?, "
            "updated_at=? WHERE synapse_id=?",
            (weight, int(row[1]) + 1, now.isoformat(), now.isoformat(), synapse_id),
        )
    return weight


def coactivate(
    left_neuron: str,
    right_neuron: str,
    *,
    delta: float = COACTIVATE_REINFORCE,
    now: datetime | None = None,
    db_path: str | None = None,
) -> bool:
    if not left_neuron or not right_neuron or left_neuron == right_neuron:
        return False
    now = now or datetime.now()
    left, right = sorted((left_neuron, right_neuron))
    with store.connect(db_path) as connection:
        row = connection.execute(
            "SELECT synapse_id, weight FROM synapses WHERE from_neuron=? AND to_neuron=?",
            (left, right),
        ).fetchone()
    if row is None:
        return network.upsert_synapse(
            {
                "from_neuron": left,
                "to_neuron": right,
                "weight": COACTIVATE_REINFORCE * 0.3,
                "base_weight": COACTIVATE_REINFORCE * 0.3,
                "relation": "coactivation",
            },
            db_path=db_path,
        )
    reinforce_synapse(int(row[0]), delta=delta, now=now, db_path=db_path)
    return True


def reinforce_after_recall(
    items: list[dict],
    *,
    now: datetime | None = None,
    db_path: str | None = None,
) -> dict:
    if not config.memory_neuron_enabled():
        return {"memories": 0, "synapses": 0}
    now = now or datetime.now()
    memories = 0
    synapses = 0
    seen_memories: set[str] = set()
    seen_synapses: set[int] = set()
    for item in items:
        memory_id = str(item.get("id") or "")
        if memory_id.startswith("notes:"):
            continue
        if memory_id and memory_id not in seen_memories:
            seen_memories.add(memory_id)
            reinforce_memory(memory_id, now=now, db_path=db_path)
            memories += 1
        for hop in item.get("path") or []:
            if not isinstance(hop, dict):
                continue
            synapse_id = hop.get("synapse_id")
            if not synapse_id or synapse_id in seen_synapses:
                continue
            seen_synapses.add(int(synapse_id))
            reinforce_synapse(int(synapse_id), now=now, db_path=db_path)
            synapses += 1
    return {"memories": memories, "synapses": synapses}


def coactivate_recalled(
    items: list[dict],
    *,
    limit: int = 5,
    db_path: str | None = None,
) -> int:
    """Link neurons returned by one recall; weak edges only, capped per call."""
    if not config.memory_neuron_enabled():
        return 0
    neurons: list[str] = []
    seen: set[str] = set()
    for item in items:
        memory_id = str(item.get("id") or "")
        if not memory_id or memory_id.startswith("notes:"):
            continue
        neuron_id = network.neuron_of(memory_id, db_path=db_path)
        if neuron_id and neuron_id not in seen:
            seen.add(neuron_id)
            neurons.append(neuron_id)
        if len(neurons) >= limit:
            break
    pairs = 0
    for left, right in itertools.combinations(neurons, 2):
        if coactivate(left, right, db_path=db_path):
            pairs += 1
    return pairs


LTD_DELTA = 0.05
STABILITY_FLOOR = 1.0


def apply_ltd(
    memory_id: str,
    *,
    delta: float = LTD_DELTA,
    now: datetime | None = None,
    db_path: str | None = None,
) -> dict:
    """Weaken the synapses around one memory and halve its stability."""
    now = now or datetime.now()
    neuron_id = network.neuron_of(memory_id, db_path=db_path)
    if not neuron_id:
        return {"synapses": 0, "stability": 0.0}
    with store.connect(db_path) as connection:
        rows = connection.execute(
            "SELECT synapse_id FROM synapses WHERE status='active' "
            "AND (from_neuron=? OR to_neuron=?)",
            (neuron_id, neuron_id),
        ).fetchall()
        for (synapse_id,) in rows:
            connection.execute(
                "UPDATE synapses SET weight=MAX(0.0, weight-?), updated_at=? "
                "WHERE synapse_id=?",
                (delta, now.isoformat(), synapse_id),
            )
        row = connection.execute(
            "SELECT stability FROM memory_activation WHERE node_id=?", (memory_id,)
        ).fetchone()
        stability = 0.0
        if row is not None:
            stability = max(STABILITY_FLOOR, float(row[0] or 30.0) / 2.0)
            connection.execute(
                "UPDATE memory_activation SET stability=? WHERE node_id=?",
                (stability, memory_id),
            )
    return {"synapses": len(rows), "stability": round(stability, 3)}


def unbind_weak(
    *,
    now: datetime | None = None,
    db_path: str | None = None,
    dry_run: bool = False,
) -> int:
    now = now or datetime.now()
    cutoff = (now - timedelta(days=UNBIND_DAYS)).isoformat()
    where = (
        "status='active' AND weight<? AND COALESCE(last_activated_at, created_at)<=?"
    )
    with store.connect(db_path) as connection:
        if dry_run:
            return int(
                connection.execute(
                    f"SELECT COUNT(*) FROM synapses WHERE {where}",
                    (UNBIND_WEIGHT, cutoff),
                ).fetchone()[0]
            )
        return connection.execute(
            f"UPDATE synapses SET status='archived', updated_at=? WHERE {where}",
            (now.isoformat(), UNBIND_WEIGHT, cutoff),
        ).rowcount
