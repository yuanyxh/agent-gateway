"""Dynamic strength for memories and synapses: decay, reinforcement and unbinding."""

from __future__ import annotations

import itertools
import math
from datetime import datetime, timedelta

from agent_gateway.core import config
from agent_gateway.core.log_utils import setup_logging
from agent_gateway.services.memory import network, store

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
    memory_factor = math.exp(-math.log(2) * days / STRENGTH_HALF_LIFE_DAYS)
    synapse_factor = math.exp(-math.log(2) * days / SYNAPSE_HALF_LIFE_DAYS)
    with store.connect(db_path) as connection:
        memory_rows = connection.execute(
            "UPDATE memory_activation SET strength=MAX(0.0, strength*?)",
            (memory_factor,),
        ).rowcount
        synapse_rows = connection.execute(
            "UPDATE synapses SET weight=MAX(0.0, weight*?) WHERE status='active'",
            (synapse_factor,),
        ).rowcount
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
