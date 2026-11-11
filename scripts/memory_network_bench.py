#!/usr/bin/env python3
"""Synthetic load test for the memory network; never touches a real store."""

from __future__ import annotations

import argparse
import json
import os
import random
import tempfile
import time

os.environ.setdefault("QDRANT_PATH", tempfile.mkdtemp(prefix="agent-gateway-bench-"))

from agent_gateway.services.memory import (  # noqa: E402
    lifecycle,
    network,
    network_build,
    strength,
    store,
)
from agent_gateway.services.retrieval.arms.associative import spread

BUDGETS = {
    "spread_p95_ms": 300.0,
    "decay_ms": 120_000.0,
    "tiers_ms": 300_000.0,
}


def _seed(db: str, nodes: int, neurons: int, synapses: int) -> None:
    now = "2026-11-10T12:00:00"
    with store.connect(db) as connection:
        connection.executemany(
            "INSERT OR IGNORE INTO neurons (neuron_id, episode_id, title, "
            "member_count, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            [
                (f"ep{index}", f"ep{index}", f"neuron {index}", 0, now, now)
                for index in range(neurons)
            ],
        )
    node_rows = []
    member_rows = []
    activation_rows = []
    for index in range(nodes):
        neuron_id = f"ep{index % neurons}"
        node_rows.append(
            {
                "node_id": f"m{index}",
                "layer": "hot",
                "status": "active",
                "title": f"memory {index}",
                "created_at": now,
                "metadata": {"episode_id": neuron_id, "importance": 5},
            }
        )
        member_rows.append((neuron_id, f"m{index}", "hot", 1.0, "member", now))
        activation_rows.append((f"m{index}", 0.5, "active", now, 1, None, None, "{}"))
    for start in range(0, len(node_rows), 1000):
        store.upsert_nodes(node_rows[start : start + 1000], db_path=db)
    with store.connect(db) as connection:
        connection.executemany(
            "INSERT OR IGNORE INTO neuron_members "
            "(neuron_id, memory_id, layer, weight, role, joined_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            member_rows,
        )
        connection.executemany(
            "INSERT OR IGNORE INTO memory_activation "
            "(node_id, strength, tier, last_activated_at, activation_count, "
            "tier_changed_at, purge_after, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            activation_rows,
        )
        connection.execute(
            "UPDATE neurons SET member_count=(SELECT COUNT(*) FROM neuron_members "
            "m WHERE m.neuron_id=neurons.neuron_id)"
        )
        existing = set()
        rows = []
        rng = random.Random(11)
        while len(rows) < synapses:
            left = rng.randrange(neurons)
            right = rng.randrange(neurons)
            if left == right:
                continue
            pair = (min(left, right), max(left, right))
            if pair in existing:
                continue
            existing.add(pair)
            rows.append(
                (
                    f"ep{pair[0]}",
                    f"ep{pair[1]}",
                    0.5,
                    0.5,
                    "same_task",
                    0,
                    "active",
                    now,
                    0,
                    0.0,
                    0.0,
                    now,
                    now,
                    "{}",
                )
            )
        connection.executemany(
            "INSERT OR IGNORE INTO synapses "
            "(from_neuron, to_neuron, weight, base_weight, relation, directed, "
            "status, last_activated_at, activation_count, decay_rate, "
            "reinforce_rate, created_at, updated_at, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )


def _ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 2)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--nodes", type=int, default=50_000)
    parser.add_argument("--neurons", type=int, default=5_000)
    parser.add_argument("--synapses", type=int, default=100_000)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "bench.sqlite")
        started = time.perf_counter()
        _seed(db, args.nodes, args.neurons, args.synapses)
        seed_ms = _ms(started)
        times = []
        for _ in range(20):
            start = time.perf_counter()
            spread(
                [{"neuron_id": "ep0", "activation": 1.0}],
                db_path=db,
                depth=3,
                beam=5,
                floor=0.15,
            )
            times.append(_ms(start))
        times.sort()
        spread_p95 = times[max(0, int(len(times) * 0.95) - 1)]
        started = time.perf_counter()
        strength.apply_decay(
            now=__import__("datetime").datetime(2026, 11, 10), db_path=db
        )
        decay_ms = _ms(started)
        started = time.perf_counter()
        lifecycle.classify_tiers(
            now=__import__("datetime").datetime(2026, 11, 10, 12), db_path=db
        )
        tiers_ms = _ms(started)
        network_build.enforce_degree_cap(db_path=db, cap=20)
        stats = network.stats(db_path=db)
        result = {
            "nodes": args.nodes,
            "neurons": args.neurons,
            "synapses": args.synapses,
            "seed_ms": seed_ms,
            "spread_p95_ms": spread_p95,
            "decay_ms": decay_ms,
            "tiers_ms": tiers_ms,
            "stats": stats,
            "budgets": BUDGETS,
        }
        failed = [
            key
            for key, budget in BUDGETS.items()
            if result.get(key, float("inf")) > budget
        ]
        if stats["degree_p95"] > 20:
            failed.append("degree_p95")
        if stats["synapses"] > args.neurons * 20:
            failed.append("synapses")
        result["failed"] = failed
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
