"""Edge writes and traversals for the memory graph.

Edges record only what vectors cannot express. Similarity stays in the vector
stores; ``relates`` is reserved for a relationship a user or a consolidation
pass asserted, not for "these two embed close together".
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from agent_gateway.core.log_utils import setup_logging
from agent_gateway.services.memory import store

logger = setup_logging("server")

EDGE_TYPES = (
    "supersedes",
    "episode",
    "derived_from",
    "disputes",
    "relates",
    "mentions",
)

#: Written once and read from either endpoint.
SYMMETRIC_EDGE_TYPES = ("episode", "disputes", "relates")

EDGE_FIELDS = ("from_id", "to_id", "type", "weight", "created_at", "metadata")

#: Per-type node ceilings; a version chain is short, an episode group is capped
#: at write time already, and an unbounded sweep would let one bad edge fan out.
MAX_NODES = {"supersedes": 200, "episode": 500, "derived_from": 500}


def _decode_edge(row: sqlite3.Row | tuple) -> dict:
    edge = dict(zip(EDGE_FIELDS, tuple(row)))
    raw = edge.get("metadata")
    try:
        edge["metadata"] = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        edge["metadata"] = {}
    return edge


def add_edge(
    from_id: str,
    to_id: str,
    type: str,
    *,
    weight: float = 1.0,
    metadata: dict | None = None,
    db_path: str | None = None,
) -> bool:
    if not from_id or not to_id or from_id == to_id:
        return False
    if type not in EDGE_TYPES:
        raise ValueError(f"unknown edge type: {type}")
    return add_edges([(from_id, to_id, type, weight, metadata)], db_path=db_path) > 0


def add_edges(edges: list[tuple], db_path: str | None = None) -> int:
    """Write every edge in one transaction; duplicates refresh the stored weight."""
    if not edges:
        return 0
    now = datetime.now().isoformat()
    rows, rejected = [], 0
    for edge in edges:
        from_id, to_id, type = edge[0], edge[1], edge[2]
        if not from_id or not to_id or from_id == to_id or type not in EDGE_TYPES:
            rejected += 1
            continue
        weight = edge[3] if len(edge) > 3 else 1.0
        metadata = edge[4] if len(edge) > 4 else None
        payload = json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True)
        rows.append((from_id, to_id, type, weight, now, payload))
    if rejected:
        logger.warning(f"[memory-graph] rejected {rejected}/{len(edges)} edges")
    if not rows:
        return 0
    try:
        with store.connect(db_path) as connection:
            connection.executemany(
                """
                INSERT INTO memory_edges
                  (from_id, to_id, type, weight, created_at, metadata)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(from_id, to_id, type) DO UPDATE SET
                  weight=excluded.weight,
                  metadata=excluded.metadata
                """,
                rows,
            )
    except sqlite3.Error as e:
        logger.warning(f"[memory-graph] edge write failed ({len(rows)} rows): {e}")
        raise
    return len(rows)


def remove_episode_edges(node_ids: list[str], db_path: str | None = None) -> int:
    """Drop stale episode edges for the given nodes before regrouping."""
    ids = [str(node_id) for node_id in node_ids if node_id]
    removed = 0
    for start in range(0, len(ids), 400):
        batch = ids[start : start + 400]
        placeholders = ",".join("?" for _ in batch)
        sql = (
            "DELETE FROM memory_edges WHERE type='episode' "
            f"AND (from_id IN ({placeholders}) OR to_id IN ({placeholders}))"
        )
        try:
            with store.connect(db_path) as connection:
                removed += connection.execute(sql, batch + batch).rowcount
        except sqlite3.Error as e:
            logger.warning(f"[graph] episode edge delete failed: {e}")
    return removed


def neighbors(
    node_id: str,
    *,
    types: tuple[str, ...] | None = None,
    direction: str = "both",
    limit: int = 200,
    db_path: str | None = None,
) -> list[dict]:
    """Edges touching one node.

    ``down`` follows from_id -> to_id (newer to older for supersedes),
    ``up`` follows to_id -> from_id, ``both`` returns either side.
    """
    clauses, params = [], []
    if direction in ("down", "both"):
        clauses.append("from_id=?")
        params.append(node_id)
    if direction in ("up", "both"):
        clauses.append("to_id=?")
        params.append(node_id)
    if not clauses:
        raise ValueError(f"invalid direction: {direction}")
    sql = (
        f"SELECT {', '.join(EDGE_FIELDS)} FROM memory_edges "
        f"WHERE ({' OR '.join(clauses)})"
    )
    if types:
        sql += f" AND type IN ({','.join('?' for _ in types)})"
        params.extend(types)
    sql += " LIMIT ?"
    params.append(limit)
    with store.connect(db_path) as connection:
        rows = connection.execute(sql, params).fetchall()
    return [_decode_edge(row) for row in rows]


def has_edge(
    from_id: str,
    to_id: str,
    type: str,
    *,
    db_path: str | None = None,
) -> bool:
    """True when the pair already carries this edge; symmetric types match either way."""
    if not from_id or not to_id or from_id == to_id or type not in EDGE_TYPES:
        return False
    if type in SYMMETRIC_EDGE_TYPES:
        sql = (
            "SELECT 1 FROM memory_edges WHERE type=? AND "
            "((from_id=? AND to_id=?) OR (from_id=? AND to_id=?)) LIMIT 1"
        )
        params = (type, from_id, to_id, to_id, from_id)
    else:
        sql = (
            "SELECT 1 FROM memory_edges WHERE from_id=? AND to_id=? AND type=? LIMIT 1"
        )
        params = (from_id, to_id, type)
    with store.connect(db_path) as connection:
        return connection.execute(sql, params).fetchone() is not None


def walk(
    node_id: str,
    edge_type: str,
    *,
    direction: str = "both",
    max_nodes: int | None = None,
    db_path: str | None = None,
) -> dict:
    """Breadth-first walk along one edge type until the chain ends or the cap hits."""
    if edge_type not in EDGE_TYPES:
        raise ValueError(f"unknown edge type: {edge_type}")
    ceiling = max_nodes or MAX_NODES.get(edge_type, 200)
    order = [node_id]
    seen = {node_id}
    edges: dict[tuple[str, str, str], dict] = {}
    frontier = [node_id]
    truncated = False
    with store.connect(db_path) as connection:
        while frontier:
            following = []
            for current in frontier:
                for edge in _edges_touching(connection, current, edge_type, direction):
                    edges[(edge["from_id"], edge["to_id"], edge["type"])] = edge
                    other = (
                        edge["to_id"] if edge["from_id"] == current else edge["from_id"]
                    )
                    if other in seen:
                        continue
                    if len(order) >= ceiling:
                        truncated = True
                        continue
                    seen.add(other)
                    order.append(other)
                    following.append(other)
            frontier = following
    nodes = store.get_nodes(order, db_path=db_path)
    return {
        "root": node_id,
        "edge": edge_type,
        "nodes": [nodes[i] for i in order if i in nodes],
        "edges": list(edges.values()),
        "missing": [node_id for node_id in order if node_id not in nodes],
        "truncated": truncated,
    }


def _edges_touching(
    connection: sqlite3.Connection, node_id: str, edge_type: str, direction: str
) -> list[dict]:
    clauses, params = [], []
    if direction in ("down", "both"):
        clauses.append("from_id=?")
        params.append(node_id)
    if direction in ("up", "both"):
        clauses.append("to_id=?")
        params.append(node_id)
    if not clauses:
        raise ValueError(f"invalid direction: {direction}")
    sql = (
        f"SELECT {', '.join(EDGE_FIELDS)} FROM memory_edges "
        f"WHERE ({' OR '.join(clauses)}) AND type=?"
    )
    params.append(edge_type)
    rows = connection.execute(sql, params).fetchall()
    return [_decode_edge(row) for row in rows]


def walk_sources(
    node_id: str,
    *,
    max_nodes: int | None = None,
    db_path: str | None = None,
) -> dict:
    """Cold summary -> its source memories -> each source's version chain."""
    ceiling = max_nodes or MAX_NODES["derived_from"]
    direct = neighbors(
        node_id,
        types=("derived_from",),
        direction="down",
        limit=ceiling,
        db_path=db_path,
    )
    order = [node_id] + [edge["to_id"] for edge in direct]
    edges: dict[tuple[str, str, str], dict] = {
        (edge["from_id"], edge["to_id"], edge["type"]): edge for edge in direct
    }
    truncated = len(order) >= ceiling
    for source in order[1:]:
        chain = walk(
            source, "supersedes", direction="both", max_nodes=50, db_path=db_path
        )
        for node in chain["nodes"]:
            if node["node_id"] not in order:
                if len(order) >= ceiling:
                    truncated = True
                    continue
                order.append(node["node_id"])
        for edge in chain["edges"]:
            edges[(edge["from_id"], edge["to_id"], edge["type"])] = edge
    nodes = store.get_nodes(order, db_path=db_path)
    return {
        "root": node_id,
        "edge": "derived_from",
        "nodes": [nodes[i] for i in order if i in nodes],
        "edges": list(edges.values()),
        "missing": [node_id for node_id in order if node_id not in nodes],
        "truncated": truncated,
    }


def count_edges(db_path: str | None = None) -> int:
    with store.connect(db_path) as connection:
        return int(
            connection.execute("SELECT COUNT(*) FROM memory_edges").fetchone()[0]
        )
