"""CSR graph index for millisecond multi-hop expansion."""

from __future__ import annotations

import time

import numpy as np

from agent_gateway.services.memory import store

MAX_EDGES = 2_000_000


class GraphIndex:
    def __init__(
        self, nodes, indptr, indices, weights, polarity, synapse_ids=None, forward=None
    ):
        self.nodes = nodes
        self.index = {name: position for position, name in enumerate(nodes)}
        self.indptr = indptr
        self.indices = indices
        self.weights = weights
        self.polarity = polarity
        if synapse_ids is None:
            synapse_ids = np.zeros(len(indices), dtype=np.int32)
        self.synapse_ids = synapse_ids
        if forward is None:
            forward = np.ones(len(indices), dtype=np.int8)
        self.forward = forward
        self.built_at = time.monotonic()

    @classmethod
    def from_rows(cls, rows) -> "GraphIndex":
        rows = list(rows)
        if not rows:
            return cls(
                [],
                np.zeros(1, dtype=np.int64),
                np.zeros(0, dtype=np.int32),
                np.zeros(0, dtype=np.float32),
                np.zeros(0, dtype=np.int8),
            )
        nodes = sorted({row[0] for row in rows} | {row[1] for row in rows})
        position = {name: index for index, name in enumerate(nodes)}
        total = len(rows) * 2
        source = np.empty(total, dtype=np.int64)
        target = np.empty(total, dtype=np.int64)
        weight = np.empty(total, dtype=np.float32)
        polarity = np.empty(total, dtype=np.int8)
        synapse_ids = np.zeros(total, dtype=np.int32)
        forward = np.zeros(total, dtype=np.int8)
        for offset, row in enumerate(rows):
            left, right, link_weight, sign = row[:4]
            if len(row) > 4:
                synapse_ids[offset * 2] = synapse_ids[offset * 2 + 1] = int(row[4] or 0)
            a, b = position[left], position[right]
            forward[offset * 2] = 1
            source[offset * 2], target[offset * 2] = a, b
            source[offset * 2 + 1], target[offset * 2 + 1] = b, a
            weight[offset * 2] = weight[offset * 2 + 1] = float(link_weight)
            polarity[offset * 2] = polarity[offset * 2 + 1] = int(sign or 1)
        order = np.argsort(source, kind="stable")
        source = source[order]
        target = target[order]
        counts = np.bincount(source, minlength=len(nodes))[: len(nodes)]
        indptr = np.zeros(len(nodes) + 1, dtype=np.int64)
        indptr[1:] = np.cumsum(counts)
        return cls(
            nodes,
            indptr,
            target.astype(np.int32),
            weight[order],
            polarity[order],
            synapse_ids[order],
            forward[order],
        )

    @classmethod
    def load(cls, db_path: str | None = None, *, max_edges: int = MAX_EDGES):
        position: dict[str, int] = {}
        nodes: list[str] = []
        with store.connect(db_path) as connection:
            total_edges = int(
                connection.execute(
                    "SELECT COUNT(*) FROM synapses WHERE status='active'"
                ).fetchone()[0]
            )
        total_edges = min(total_edges, max_edges)
        source = np.empty(total_edges * 2, dtype=np.int32)
        target = np.empty(total_edges * 2, dtype=np.int32)
        weight = np.empty(total_edges * 2, dtype=np.float32)
        polarity = np.empty(total_edges * 2, dtype=np.int8)
        synapse_ids = np.zeros(total_edges * 2, dtype=np.int32)
        forward = np.zeros(total_edges * 2, dtype=np.int8)
        used = 0
        with store.connect(db_path) as connection:
            cursor = connection.execute(
                "SELECT from_neuron, to_neuron, weight, COALESCE(polarity, 1), "
                "synapse_id FROM synapses WHERE status='active' LIMIT ?",
                (max_edges,),
            )
            for left, right, link_weight, sign, synapse_id in cursor:
                if used + 2 > len(source):
                    break
                a = position.get(left)
                if a is None:
                    a = len(nodes)
                    position[left] = a
                    nodes.append(left)
                b = position.get(right)
                if b is None:
                    b = len(nodes)
                    position[right] = b
                    nodes.append(right)
                value = float(link_weight or 0.0)
                flag = int(sign or 1)
                forward[used] = 1
                source[used], target[used] = a, b
                weight[used], polarity[used] = value, flag
                source[used + 1], target[used + 1] = b, a
                weight[used + 1], polarity[used + 1] = value, flag
                synapse_ids[used] = synapse_ids[used + 1] = int(synapse_id or 0)
                used += 2
        source = source[:used]
        target = target[:used]
        weight = weight[:used]
        polarity = polarity[:used]
        synapse_ids = synapse_ids[:used]
        forward = forward[:used]
        order = np.argsort(source, kind="stable")
        source = source[order]
        target = target[order]
        weight = weight[order]
        polarity = polarity[order]
        synapse_ids = synapse_ids[order]
        forward = forward[order]
        counts = np.bincount(source, minlength=len(nodes))[: len(nodes)]
        indptr = np.zeros(len(nodes) + 1, dtype=np.int64)
        indptr[1:] = np.cumsum(counts)
        return cls(nodes, indptr, target, weight, polarity, synapse_ids, forward)

    def degree(self, neuron_id: str) -> int:
        position = self.index.get(neuron_id)
        if position is None:
            return 0
        return int(self.indptr[position + 1] - self.indptr[position])

    def neighbors(self, neuron_id: str, *, limit: int = 8) -> list[dict]:
        """Outgoing and incoming links, each capped at `limit` like the SQL path."""
        position = self.index.get(neuron_id)
        if position is None:
            return []
        start = int(self.indptr[position])
        end = int(self.indptr[position + 1])
        if end <= start:
            return []
        span = end - start
        weights = self.weights[start:end]
        forward = self.forward[start:end]
        ids = self.synapse_ids[start:end].astype(np.int64)
        links: list[dict] = []
        per_direction = min(max(1, limit), span)
        for wanted in (1, 0):
            offsets = np.nonzero(forward == wanted)[0]
            if offsets.size == 0:
                continue
            order = offsets[np.lexsort((ids[offsets], -weights[offsets]))]
            for offset in order[:per_direction]:
                index = start + int(offset)
                links.append(
                    {
                        "other": self.nodes[int(self.indices[index])],
                        "weight": float(self.weights[index]),
                        "polarity": int(self.polarity[index]),
                        "synapse_id": int(self.synapse_ids[index]),
                    }
                )
        return links

    def expand(
        self,
        neuron_id: str,
        *,
        hops: int = 2,
        limit: int = 20,
        budget: int = 500,
        decay: float = 0.85,
    ) -> dict:
        start = self.index.get(neuron_id)
        if start is None:
            return {
                "neurons": [],
                "visited": 0,
                "budget_exhausted": False,
                "unknown": True,
            }
        activation = {start: 1.0}
        paths = {start: [self.nodes[start]]}
        frontier = [start]
        visited = {start}
        budget_exhausted = False
        for _ in range(max(0, hops)):
            following: dict[int, float] = {}
            for node in frontier:
                for offset in range(self.indptr[node], self.indptr[node + 1]):
                    nxt = int(self.indices[offset])
                    if nxt in visited:
                        continue
                    value = activation[node] * float(self.weights[offset]) * decay
                    if self.polarity[offset] < 0:
                        value = -value
                    if value <= 0:
                        continue
                    if value > following.get(nxt, 0.0):
                        following[nxt] = value
                        paths[nxt] = paths[node] + [self.nodes[nxt]]
            room = budget - len(visited)
            if room <= 0:
                budget_exhausted = True
                break
            if len(following) > room:
                following = dict(
                    sorted(following.items(), key=lambda item: -item[1])[:room]
                )
                budget_exhausted = True
            visited.update(following)
            activation.update(following)
            frontier = sorted(following, key=lambda item: -following[item])[:limit]
            if not frontier:
                break
        ordered = sorted(
            ((node, value) for node, value in activation.items() if node != start),
            key=lambda item: -item[1],
        )[:limit]
        return {
            "neurons": [
                {
                    "neuron_id": self.nodes[node],
                    "activation": round(value, 4),
                    "path": paths[node],
                }
                for node, value in ordered
            ],
            "visited": len(visited),
            "budget_exhausted": budget_exhausted,
            "unknown": False,
        }


REBUILD_SECONDS = 30.0

_cache: dict[str, tuple[float, GraphIndex]] = {}


def current(db_path: str | None = None) -> GraphIndex:
    """Rebuild at most once per REBUILD_SECONDS; unrelated table writes never count."""
    path = db_path or str(store.graph_db_path())
    cached = _cache.get(path)
    if cached is not None:
        index = cached[1]
        if time.monotonic() - index.built_at < REBUILD_SECONDS:
            return index
    index = GraphIndex.load(db_path)
    _cache[path] = (time.monotonic(), index)
    return index


def reset() -> None:
    _cache.clear()
