#!/usr/bin/env python3
"""Memory-graph evaluation baseline: topology, sampling and trace metrics."""

from __future__ import annotations

import argparse
import json
import os
import random
from collections import Counter, defaultdict, deque

from agent_gateway.services.memory import store
from agent_gateway.services.retrieval import trace


def _percentile(values: list[float], ratio: float) -> float:
    if not values:
        return 0.0
    index = min(len(values) - 1, int(round(ratio * (len(values) - 1))))
    return round(float(values[index]), 2)


def _bfs(adjacency: dict[str, set[str]], start: str) -> dict[str, int]:
    dist = {start: 0}
    queue = deque([start])
    while queue:
        current = queue.popleft()
        for neighbor in adjacency[current]:
            if neighbor not in dist:
                dist[neighbor] = dist[current] + 1
                queue.append(neighbor)
    return dist


def topology(connection) -> dict:
    neurons = {
        row[0]
        for row in connection.execute(
            "SELECT neuron_id FROM neurons WHERE status='active'"
        )
    }
    edges = [
        (left, right)
        for left, right in connection.execute(
            "SELECT from_neuron, to_neuron FROM synapses WHERE status='active'"
        )
        if left in neurons and right in neurons
    ]
    adjacency: dict[str, set[str]] = defaultdict(set)
    for left, right in edges:
        adjacency[left].add(right)
        adjacency[right].add(left)
    components: list[list[str]] = []
    seen: set[str] = set()
    for start in sorted(adjacency):
        if start in seen:
            continue
        stack = [start]
        seen.add(start)
        members: list[str] = []
        while stack:
            current = stack.pop()
            members.append(current)
            for neighbor in adjacency[current]:
                if neighbor not in seen:
                    seen.add(neighbor)
                    stack.append(neighbor)
        components.append(members)
    diameter = 0
    if components:
        largest = max(components, key=len)
        dist = _bfs(adjacency, min(largest))
        far = max(sorted(dist), key=dist.get)
        diameter = max(_bfs(adjacency, far).values())
    sizes = sorted((len(item) for item in components), reverse=True)
    total = len(neurons)
    return {
        "neurons": total,
        "edges": len(edges),
        "nodes_with_edges": len(adjacency),
        "isolated": total - len(adjacency),
        "isolated_ratio": round((total - len(adjacency)) / total, 4) if total else 0.0,
        "components": sizes,
        "diameter": diameter,
        "avg_degree": round(2 * len(edges) / total, 2) if total else 0.0,
    }


def _trace_metrics(rows: list[dict]) -> dict:
    if not rows:
        return {"status": "empty"}
    status = Counter(str(row.get("status") or "?") for row in rows)
    latencies = sorted(
        float((row.get("latency_ms") or {}).get("total") or 0.0) for row in rows
    )
    counts = sorted(int(row.get("count") or 0) for row in rows)
    return {
        "calls": len(rows),
        "status_counts": dict(sorted(status.items())),
        "latency_p50_ms": _percentile(latencies, 0.5),
        "latency_p95_ms": _percentile(latencies, 0.95),
        "count_p50": _percentile(counts, 0.5),
    }


def collect(trace_rows: list[dict], connection) -> dict:
    return {"trace": _trace_metrics(trace_rows), "topology": topology(connection)}


def sample_neurons(
    connection,
    *,
    size: int = 400,
    seed: int = 20261111,
    per_stratum: int = 15,
) -> dict:
    neurons = sorted(
        row[0]
        for row in connection.execute(
            "SELECT neuron_id FROM neurons WHERE status='active'"
        )
    )
    linked: set[str] = set()
    for left, right in connection.execute(
        "SELECT from_neuron, to_neuron FROM synapses WHERE status='active'"
    ):
        linked.add(left)
        linked.add(right)
    members = dict(
        connection.execute(
            "SELECT neuron_id, COUNT(*) FROM neuron_members GROUP BY neuron_id"
        )
    )
    oldest = dict(
        connection.execute(
            "SELECT m.neuron_id, MIN(n.created_at) FROM neuron_members m "
            "JOIN memory_nodes n ON n.node_id = m.memory_id "
            "WHERE n.status='active' GROUP BY m.neuron_id"
        )
    )
    strata: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    for neuron_id in neurons:
        month = str(oldest.get(neuron_id) or "")[:7] or "unknown"
        state = "linked" if neuron_id in linked else "isolated"
        count = int(members.get(neuron_id, 0))
        bucket = "1" if count <= 1 else ("2-8" if count <= 8 else "9+")
        strata[(month, state, bucket)].append(neuron_id)
    oldest_month = min((key[0] for key in strata if key[0] != "unknown"), default="")
    rng = random.Random(seed)
    total = max(1, len(neurons))
    chosen: list[str] = []
    for key in sorted(strata):
        group = strata[key]
        quota = (
            len(group)
            if key[0] == oldest_month
            else max(per_stratum, round(size * len(group) / total))
        )
        chosen.extend(rng.sample(group, min(len(group), quota)))
    return {
        "neurons": sorted(chosen),
        "strata_covered": sorted({key[0] for key in strata}),
        "strata": {
            f"{month}|{state}|{bucket}": len(group)
            for (month, state, bucket), group in sorted(strata.items())
        },
        "requested": size,
    }


def load_trace(directory: str, limit: int = 0) -> list[dict]:
    rows: list[dict] = []
    if not os.path.isdir(directory):
        return rows
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".jsonl"):
            continue
        with open(os.path.join(directory, name), encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        continue
    return rows[-limit:] if limit else rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(store.graph_db_path()))
    parser.add_argument("--trace-dir", default=trace.trace_dir())
    parser.add_argument("--trace-limit", type=int, default=0)
    parser.add_argument("--sample", type=int, default=0)
    parser.add_argument("--seed", type=int, default=20261111)
    parser.add_argument("--out", default="")
    args = parser.parse_args()
    rows = load_trace(args.trace_dir, args.trace_limit)
    with store.connect(args.db) as connection:
        result = collect(rows, connection)
        if args.sample:
            result["sample"] = sample_neurons(
                connection, size=args.sample, seed=args.seed
            )
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(payload + "\n")
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
