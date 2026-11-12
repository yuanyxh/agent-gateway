"""SQLite node registry behind the memory graph.

Vectors and bodies stay in their own stores; this table carries identity,
lifecycle, and the fields a traversal needs. Node ids are stable business keys:
the Mem0 id for hot rows, ``cold:`` + sha1 for cold points.
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

#: Bump whenever SCHEMA gains a table or index; `connect` only replays DDL below it.
SCHEMA_VERSION = 3

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
  valid_from TEXT,
  valid_to TEXT,
  supports INTEGER DEFAULT 1,
  contradicts INTEGER DEFAULT 0,
  UNIQUE(from_id, to_id, type)
);
CREATE INDEX IF NOT EXISTS idx_edges_from ON memory_edges(from_id);
CREATE INDEX IF NOT EXISTS idx_edges_to ON memory_edges(to_id);

CREATE TABLE IF NOT EXISTS neurons (
  neuron_id TEXT PRIMARY KEY,
  episode_id TEXT UNIQUE,
  title TEXT,
  summary TEXT,
  centroid BLOB,
  member_count INTEGER NOT NULL DEFAULT 0,
  tier TEXT NOT NULL DEFAULT 'active',
  status TEXT NOT NULL DEFAULT 'active',
  heat REAL NOT NULL DEFAULT 0.0,
  importance REAL NOT NULL DEFAULT 0.0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  metadata TEXT,
  kind TEXT DEFAULT 'episodic',
  sparse_code BLOB,
  context_key TEXT,
  specificity REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_neurons_tier ON neurons(tier);
CREATE INDEX IF NOT EXISTS idx_neurons_status ON neurons(status);

CREATE TABLE IF NOT EXISTS neuron_members (
  neuron_id TEXT NOT NULL,
  memory_id TEXT NOT NULL,
  layer TEXT NOT NULL DEFAULT 'hot',
  weight REAL NOT NULL DEFAULT 1.0,
  role TEXT NOT NULL DEFAULT 'member',
  joined_at TEXT NOT NULL,
  PRIMARY KEY (neuron_id, memory_id)
);
CREATE INDEX IF NOT EXISTS idx_members_memory ON neuron_members(memory_id);

CREATE TABLE IF NOT EXISTS synapses (
  synapse_id INTEGER PRIMARY KEY AUTOINCREMENT,
  from_neuron TEXT NOT NULL,
  to_neuron TEXT NOT NULL,
  weight REAL NOT NULL DEFAULT 0.0,
  base_weight REAL NOT NULL DEFAULT 0.0,
  relation TEXT NOT NULL DEFAULT '',
  directed INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'active',
  last_activated_at TEXT,
  activation_count INTEGER NOT NULL DEFAULT 0,
  decay_rate REAL NOT NULL DEFAULT 0.0,
  reinforce_rate REAL NOT NULL DEFAULT 0.0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  metadata TEXT,
  polarity INTEGER DEFAULT 1,
  stability REAL DEFAULT 90,
  kind TEXT DEFAULT 'assoc',
  inferred INTEGER DEFAULT 0,
  UNIQUE(from_neuron, to_neuron)
);
CREATE INDEX IF NOT EXISTS idx_syn_from ON synapses(from_neuron);
CREATE INDEX IF NOT EXISTS idx_syn_to ON synapses(to_neuron);
CREATE INDEX IF NOT EXISTS idx_syn_status ON synapses(status);

CREATE TABLE IF NOT EXISTS memory_activation (
  node_id TEXT PRIMARY KEY,
  strength REAL NOT NULL DEFAULT 0.0,
  tier TEXT NOT NULL DEFAULT 'active',
  last_activated_at TEXT,
  activation_count INTEGER NOT NULL DEFAULT 0,
  tier_changed_at TEXT,
  purge_after TEXT,
  metadata TEXT,
  stability REAL DEFAULT 30,
  difficulty REAL DEFAULT 5,
  unstable_until TEXT
);
CREATE INDEX IF NOT EXISTS idx_activation_tier ON memory_activation(tier);
CREATE INDEX IF NOT EXISTS idx_activation_purge ON memory_activation(purge_after);

CREATE TABLE IF NOT EXISTS network_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entities (
  entity_id TEXT PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  type TEXT,
  aliases TEXT,
  metadata TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entity_mentions (
  entity_id TEXT NOT NULL,
  memory_id TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'mention',
  confidence REAL DEFAULT 1.0,
  created_at TEXT NOT NULL,
  PRIMARY KEY (entity_id, memory_id, role)
);
CREATE INDEX IF NOT EXISTS idx_mentions_memory ON entity_mentions(memory_id);

CREATE TABLE IF NOT EXISTS activation_buffer (
  neuron_id TEXT PRIMARY KEY,
  activation REAL NOT NULL DEFAULT 0.0,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS replay_runs (
  run_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  started_at TEXT NOT NULL,
  ended_at TEXT,
  sampled INTEGER DEFAULT 0,
  reinforced INTEGER DEFAULT 0,
  inferred INTEGER DEFAULT 0,
  promoted INTEGER DEFAULT 0,
  cost_tokens INTEGER DEFAULT 0,
  metadata TEXT
);
"""

#: Created after `_ensure_columns`, so old tables get their new columns first.
INDEXES = """
CREATE INDEX IF NOT EXISTS idx_neurons_kind ON neurons(kind);
CREATE INDEX IF NOT EXISTS idx_syn_kind ON synapses(kind, polarity);
CREATE INDEX IF NOT EXISTS idx_entities_name ON entities(name);
CREATE INDEX IF NOT EXISTS idx_buffer_updated ON activation_buffer(updated_at);
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


_COLD_POINT_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "agent-gateway/memory/cold")


def cold_point_id(source_file: str, content: str) -> str:
    """Qdrant needs a UUID or an int, so the deterministic id is derived, not the sha1."""
    return str(
        uuid.uuid5(
            _COLD_POINT_NAMESPACE, cold_node_id(source_file, content_hash(content))
        )
    )


def table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }


#: Columns added after v1; old databases get them via ALTER, new ones already have them.
V3_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    "neurons": (
        ("kind", "TEXT DEFAULT 'episodic'"),
        ("sparse_code", "BLOB"),
        ("context_key", "TEXT"),
        ("specificity", "REAL DEFAULT 0"),
    ),
    "synapses": (
        ("polarity", "INTEGER DEFAULT 1"),
        ("stability", "REAL DEFAULT 90"),
        ("kind", "TEXT DEFAULT 'assoc'"),
        ("inferred", "INTEGER DEFAULT 0"),
    ),
    "memory_activation": (
        ("stability", "REAL DEFAULT 30"),
        ("difficulty", "REAL DEFAULT 5"),
        ("unstable_until", "TEXT"),
    ),
    "memory_edges": (
        ("valid_from", "TEXT"),
        ("valid_to", "TEXT"),
        ("supports", "INTEGER DEFAULT 1"),
        ("contradicts", "INTEGER DEFAULT 0"),
    ),
}


def ensure_columns(connection: sqlite3.Connection) -> list[str]:
    """Add missing v3 columns; returns the `table.column` names it created."""
    tables = table_names(connection)
    added: list[str] = []
    for table, columns in V3_COLUMNS.items():
        if table not in tables:
            continue
        existing = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        for name, ddl in columns:
            if name not in existing:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
                added.append(f"{table}.{name}")
    return added


@contextmanager
def connect(
    db_path: str | None = None, *, readonly: bool = False
) -> Iterator[sqlite3.Connection]:
    path = db_path or str(graph_db_path())
    if readonly:
        with readonly_connect(path) as connection:
            yield connection
        return
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with sqlite_conn(path, timeout=10.0) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=5000")
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version < SCHEMA_VERSION:
            connection.executescript(SCHEMA)
            ensure_columns(connection)
            connection.executescript(INDEXES)
            connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        yield connection


@contextmanager
def readonly_connect(db_path: str) -> Iterator[sqlite3.Connection]:
    """Open an existing graph without creating files or writing its WAL."""
    path = Path(db_path)
    if not path.is_file():
        raise FileNotFoundError(str(path))
    uri = path.resolve().as_uri() + "?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True, timeout=5.0)
    try:
        yield connection
    finally:
        connection.close()


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
    offset: int = 0,
    readonly: bool = False,
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
    sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    path = db_path or str(graph_db_path())
    opener = readonly_connect if readonly else connect
    with opener(path) as connection:
        rows = connection.execute(sql, params).fetchall()
    return [node for node in (_decode(row) for row in rows) if node]


def count_nodes(db_path: str | None = None) -> int:
    with connect(db_path) as connection:
        return int(
            connection.execute("SELECT COUNT(*) FROM memory_nodes").fetchone()[0]
        )
