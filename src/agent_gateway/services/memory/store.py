"""SQLite node registry behind the memory graph.

Vectors and bodies stay in their own stores; this table carries identity,
lifecycle, and the fields a traversal needs. Node ids are stable business keys:
the Mem0 id for hot rows, ``cold:`` + sha1 for cold points, ``note:`` + relative
path for notes.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from agent_gateway.core.db import conn as sqlite_conn
from agent_gateway.core.log_utils import setup_logging
from agent_gateway.core.paths import gateway_home

logger = setup_logging("server")

SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_nodes (
  node_id TEXT PRIMARY KEY,
  layer TEXT NOT NULL,
  type TEXT,
  status TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  title TEXT,
  source_file TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  metadata TEXT
);
CREATE INDEX IF NOT EXISTS idx_nodes_hash ON memory_nodes(content_hash);
CREATE INDEX IF NOT EXISTS idx_nodes_status ON memory_nodes(status);

CREATE TABLE IF NOT EXISTS memory_edges (
  edge_id INTEGER PRIMARY KEY AUTOINCREMENT,
  from_id TEXT NOT NULL,
  to_id TEXT NOT NULL,
  type TEXT NOT NULL,
  weight REAL DEFAULT 1.0,
  created_at TEXT NOT NULL,
  metadata TEXT,
  UNIQUE(from_id, to_id, type)
);
CREATE INDEX IF NOT EXISTS idx_edges_from ON memory_edges(from_id);
CREATE INDEX IF NOT EXISTS idx_edges_to ON memory_edges(to_id);
"""

NODE_FIELDS = (
    "node_id",
    "layer",
    "type",
    "status",
    "content_hash",
    "title",
    "source_file",
    "created_at",
    "updated_at",
    "metadata",
)

_UPSERT_NODE_SQL = """
INSERT INTO memory_nodes
  (node_id, layer, type, status, content_hash, title, source_file,
   created_at, updated_at, metadata)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(node_id) DO UPDATE SET
  layer=excluded.layer,
  type=excluded.type,
  status=excluded.status,
  content_hash=excluded.content_hash,
  title=excluded.title,
  source_file=excluded.source_file,
  created_at=CASE
    WHEN memory_nodes.created_at != '' THEN memory_nodes.created_at
    ELSE excluded.created_at END,
  updated_at=excluded.updated_at,
  metadata=excluded.metadata
"""


def graph_db_path() -> Path:
    """Graph database lives beside the gateway state, separate from the vectors."""
    return gateway_home() / "memory_graph.sqlite"


def content_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def cold_node_id(source_file: str, body_hash: str) -> str:
    """Deterministic id for a cold point: the same source and body map to one node."""
    return (
        "cold:" + hashlib.sha1(f"{source_file}|{body_hash}".encode("utf-8")).hexdigest()
    )


def note_node_id(rel_path: str) -> str:
    return "note:" + rel_path.replace("\\", "/")


_COLD_POINT_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "agent-gateway/memory/cold")


def cold_point_id(source_file: str, content: str) -> str:
    """Qdrant needs a UUID or an int, so the deterministic id is derived, not the sha1."""
    return str(
        uuid.uuid5(
            _COLD_POINT_NAMESPACE, cold_node_id(source_file, content_hash(content))
        )
    )


@contextmanager
def connect(db_path: str | None = None) -> Iterator[sqlite3.Connection]:
    path = db_path or str(graph_db_path())
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with sqlite_conn(path, timeout=10.0) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=5000")
        connection.executescript(SCHEMA)
        yield connection


def _decode(row: sqlite3.Row | tuple | None) -> dict | None:
    if row is None:
        return None
    node = dict(zip(NODE_FIELDS, tuple(row)))
    raw = node.get("metadata")
    try:
        node["metadata"] = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        logger.warning(f"[memory-store] corrupt metadata for {node.get('node_id')}")
        node["metadata"] = {}
    return node


def upsert_node(
    node_id: str,
    *,
    layer: str,
    status: str,
    content_hash_value: str = "",
    type: str = "",
    title: str = "",
    source_file: str = "",
    created_at: str = "",
    updated_at: str = "",
    metadata: dict | None = None,
    db_path: str | None = None,
) -> None:
    """Insert or refresh one node; an empty created_at keeps the stored value."""
    now = updated_at or datetime.now().isoformat()
    payload = json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True)
    with connect(db_path) as connection:
        connection.execute(
            _UPSERT_NODE_SQL,
            (
                node_id,
                layer,
                type,
                status,
                content_hash_value,
                title,
                source_file,
                created_at or now,
                now,
                payload,
            ),
        )


def upsert_nodes(nodes: list[dict], db_path: str | None = None) -> int:
    """Batch upsert for backfills; each dict uses the same keys as `upsert_node`."""
    if not nodes:
        return 0
    now = datetime.now().isoformat()
    rows = []
    for node in nodes:
        rows.append(
            (
                node["node_id"],
                node.get("layer", ""),
                node.get("type", ""),
                node.get("status", "active"),
                node.get("content_hash_value", ""),
                node.get("title", ""),
                node.get("source_file", ""),
                node.get("created_at") or now,
                node.get("updated_at") or now,
                json.dumps(
                    node.get("metadata") or {}, ensure_ascii=False, sort_keys=True
                ),
            )
        )
    with connect(db_path) as connection:
        connection.executemany(_UPSERT_NODE_SQL, rows)
    return len(rows)


def set_status(node_id: str, status: str, db_path: str | None = None) -> bool:
    """Update lifecycle status only; missing rows are a no-op, not an error."""
    try:
        with connect(db_path) as connection:
            cursor = connection.execute(
                "UPDATE memory_nodes SET status=?, updated_at=? WHERE node_id=?",
                (status, datetime.now().isoformat(), node_id),
            )
            return cursor.rowcount > 0
    except sqlite3.Error as e:
        logger.warning(f"[memory-store] status update failed for {node_id}: {e}")
        return False


def update_metadata(node_id: str, updates: dict, db_path: str | None = None) -> bool:
    """Patch selected metadata keys in place; other keys and the body stay untouched."""
    try:
        with connect(db_path) as connection:
            cursor = connection.execute(
                "UPDATE memory_nodes SET metadata=json_patch(metadata, ?), updated_at=? "
                "WHERE node_id=?",
                (
                    json.dumps(updates, ensure_ascii=False),
                    datetime.now().isoformat(),
                    node_id,
                ),
            )
            return cursor.rowcount > 0
    except sqlite3.Error as e:
        logger.warning(f"[memory-store] metadata update failed for {node_id}: {e}")
        return False


def batch_update_metadata(
    updates: list[tuple[str, dict]], db_path: str | None = None
) -> int:
    """Patch many nodes in one transaction; a scoring pass must not open N connections."""
    if not updates:
        return 0
    now = datetime.now().isoformat()
    rows = [
        (json.dumps(payload, ensure_ascii=False), now, node_id)
        for node_id, payload in updates
    ]
    try:
        with connect(db_path) as connection:
            connection.executemany(
                "UPDATE memory_nodes SET metadata=json_patch(metadata, ?), updated_at=? "
                "WHERE node_id=?",
                rows,
            )
        return len(rows)
    except sqlite3.Error as e:
        logger.warning(
            f"[memory-store] batch metadata update failed ({len(rows)} rows): {e}"
        )
        return 0


def vacuum(db_path: str | None = None) -> None:
    """Reclaim space after archival; safe on WAL databases."""
    try:
        with connect(db_path) as connection:
            connection.execute("VACUUM")
    except sqlite3.Error as e:
        logger.warning(f"[memory-store] vacuum failed: {e}")


def get_node(node_id: str, db_path: str | None = None) -> dict | None:
    with connect(db_path) as connection:
        row = connection.execute(
            f"SELECT {', '.join(NODE_FIELDS)} FROM memory_nodes WHERE node_id=?",
            (node_id,),
        ).fetchone()
    return _decode(row)


def get_nodes(node_ids: list[str], db_path: str | None = None) -> dict[str, dict]:
    ids = [node_id for node_id in dict.fromkeys(node_ids) if node_id]
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    with connect(db_path) as connection:
        rows = connection.execute(
            f"SELECT {', '.join(NODE_FIELDS)} FROM memory_nodes "
            f"WHERE node_id IN ({placeholders})",
            ids,
        ).fetchall()
    return {node["node_id"]: node for node in (_decode(row) for row in rows) if node}


def find_node_by_point_id(point_id: str, db_path: str | None = None) -> dict | None:
    """Cold rows are addressed by their Qdrant point id in tool results."""
    if not point_id:
        return None
    with connect(db_path) as connection:
        row = connection.execute(
            f"SELECT {', '.join(NODE_FIELDS)} FROM memory_nodes "
            "WHERE json_extract(metadata, '$.point_id')=? "
            "OR json_extract(metadata, '$.legacy_id')=? "
            "OR EXISTS (SELECT 1 FROM json_each(memory_nodes.metadata, '$.legacy_ids') "
            "WHERE json_each.value=?) LIMIT 1",
            (point_id, point_id, point_id),
        ).fetchone()
    return _decode(row)


def find_nodes_by_metadata(
    key: str,
    value: str,
    *,
    layer: str = "",
    exclude: str = "",
    limit: int = 200,
    db_path: str | None = None,
) -> list[dict]:
    sql = (
        f"SELECT {', '.join(NODE_FIELDS)} FROM memory_nodes "
        "WHERE json_extract(metadata, ?)=?"
    )
    params: list[object] = [f"$.{key}", value]
    if layer:
        sql += " AND layer=?"
        params.append(layer)
    if exclude:
        sql += " AND node_id!=?"
        params.append(exclude)
    sql += " LIMIT ?"
    params.append(limit)
    with connect(db_path) as connection:
        rows = connection.execute(sql, params).fetchall()
    return [node for node in (_decode(row) for row in rows) if node]


def list_nodes(
    *,
    layer: str = "",
    status: str = "",
    limit: int = 500,
    db_path: str | None = None,
) -> list[dict]:
    sql = f"SELECT {', '.join(NODE_FIELDS)} FROM memory_nodes WHERE 1=1"
    params: list[object] = []
    if layer:
        sql += " AND layer=?"
        params.append(layer)
    if status:
        sql += " AND status=?"
        params.append(status)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    with connect(db_path) as connection:
        rows = connection.execute(sql, params).fetchall()
    return [node for node in (_decode(row) for row in rows) if node]


def count_nodes(db_path: str | None = None) -> int:
    with connect(db_path) as connection:
        return int(
            connection.execute("SELECT COUNT(*) FROM memory_nodes").fetchone()[0]
        )
