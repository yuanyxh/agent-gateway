"""Inhibitory attractor over neuron synapses.

Each round keeps the top-k activations, applies signed weights, and prunes by a
relative threshold so a chain does not die just because absolute values decayed.
Hub nodes restart activation, which is what lets a five-hop chain keep going.
"""

from __future__ import annotations

import os

from agent_gateway.services.memory import graph_index, network, store

STEPS = 10
TOP_K = 32
ALPHA = 0.35
BETA = 0.55
GAMMA = 0.10
RELATIVE = 0.02
RESTART = 0.6
FLOOR = 0.005
HUB_DEGREE = 8
EDGES_PER_NEURON = 8
MAX_ACTIVE = 200

_degree_cache: dict[tuple[str, float], dict[str, int]] = {}


def _degrees(db_path: str | None) -> dict[str, int]:
    path = db_path or str(store.graph_db_path())
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = 0.0
    key = (path, mtime)
    cached = _degree_cache.get(key)
    if cached is not None:
        return cached
    with store.connect(db_path) as connection:
        rows = connection.execute(
            "SELECT from_neuron, to_neuron FROM synapses WHERE status='active'"
        ).fetchall()
    counts: dict[str, int] = {}
    for left, right in rows:
        counts[left] = counts.get(left, 0) + 1
        counts[right] = counts.get(right, 0) + 1
    _degree_cache.clear()
    _degree_cache[key] = counts
    return counts


def _index(db_path: str | None):
    try:
        return graph_index.current(db_path)
    except Exception:  # noqa: BLE001 - the SQL path stays as the fallback
        return None


def activate(
    seeds: dict[str, float],
    *,
    db_path: str | None = None,
    steps: int = STEPS,
    k: int = TOP_K,
    alpha: float = ALPHA,
    beta: float = BETA,
    gamma: float = GAMMA,
    relative: float = RELATIVE,
    restart: float = RESTART,
    floor: float = FLOOR,
    hub_degree: int = HUB_DEGREE,
    edges_per_neuron: int = EDGES_PER_NEURON,
    max_active: int = MAX_ACTIVE,
) -> dict:
    activation = {
        str(key): float(value) for key, value in seeds.items() if key and value > 0
    }
    empty = {"activated": [], "rounds": 0, "converged": True, "budget_exhausted": False}
    if not activation:
        return empty
    peak = max(activation.values())
    activation = {key: value / peak for key, value in activation.items()}
    source = dict(activation)
    index = _index(db_path)
    degrees = _degrees(db_path) if index is None else {}
    rounds = 0
    converged = False
    budget_hit = False
    for step in range(max(1, steps)):
        rounds = step + 1
        frontier = sorted(activation.items(), key=lambda item: -item[1])[: max(1, k)]
        if index is None:
            links = network.synapses_of_many(
                [neuron_id for neuron_id, _ in frontier],
                limit=edges_per_neuron,
                db_path=db_path,
            )
        else:
            links = {
                neuron_id: index.neighbors(neuron_id, limit=edges_per_neuron)
                for neuron_id, _ in frontier
            }
        incoming: dict[str, float] = {}
        for neuron_id, value in frontier:
            for link in links.get(neuron_id, []):
                other = str(link.get("other") or "")
                if not other:
                    continue
                polarity = int(link.get("polarity") or 1)
                incoming[other] = incoming.get(other, 0.0) + (
                    polarity * float(link["weight"]) * value
                )
        updated: dict[str, float] = {}
        for neuron_id in set(activation) | set(incoming):
            value = (
                alpha * activation.get(neuron_id, 0.0)
                + beta * incoming.get(neuron_id, 0.0)
                + gamma * source.get(neuron_id, 0.0)
            )
            if value > 0:
                updated[neuron_id] = value
        peak = max(updated.values(), default=0.0)
        if peak <= 0:
            break
        updated = {key: value / peak for key, value in updated.items()}
        kept = {
            key: value
            for key, value in updated.items()
            if value >= relative or key in source
        }
        kept = {key: value for key, value in kept.items() if value >= floor}
        for key in kept:
            degree = index.degree(key) if index is not None else degrees.get(key, 0)
            if degree >= hub_degree:
                kept[key] = max(kept[key], restart)
        if len(kept) > max_active:
            kept = dict(sorted(kept.items(), key=lambda item: -item[1])[:max_active])
            budget_hit = True
        added = set(kept) - set(activation)
        delta = sum(
            abs(kept.get(key, 0.0) - activation.get(key, 0.0))
            for key in set(kept) | set(activation)
        )
        activation = kept
        if not added and delta < 0.01:
            converged = True
            break
    ordered = sorted(activation.items(), key=lambda item: -item[1])
    return {
        "activated": [
            {"neuron_id": neuron_id, "activation": round(value, 4)}
            for neuron_id, value in ordered
        ],
        "rounds": rounds,
        "converged": converged,
        "budget_exhausted": budget_hit,
    }
