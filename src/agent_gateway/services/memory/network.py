"""Neuron, membership, synapse and activation storage for the memory network.

`neuron_members` and `memory_activation` are the single sources of truth;
`episode_id` in mem0 and graph metadata is a compatibility projection.
"""

from __future__ import annotations

import json
from datetime import datetime

import numpy as np

from agent_gateway.core.log_utils import setup_logging
from agent_gateway.services.memory import store

logger = setup_logging("server")

NEURON_FIELDS = (
    "neuron_id",
    "episode_id",
    "title",
    "summary",
    "centroid",
    "member_count",
    "tier",
    "status",
    "heat",
    "importance",
    "created_at",
    "updated_at",
    "metadata",
)
SYNAPSE_FIELDS = (
    "synapse_id",
    "from_neuron",
    "to_neuron",
    "weight",
    "base_weight",
    "relation",
    "directed",
    "status",
    "last_activated_at",
    "activation_count",
    "decay_rate",
    "reinforce_rate",
    "created_at",
    "updated_at",
    "metadata",
)
ACTIVATION_FIELDS = (
    "node_id",
    "strength",
    "tier",
    "last_activated_at",
    "activation_count",
    "tier_changed_at",
    "purge_after",
    "metadata",
)
ACTIVATION_PATCH_KEYS = ACTIVATION_FIELDS[1:]
TIERS = ("active", "warm", "archive", "trash")


def _now(now: datetime | str | None = None) -> str:
    if isinstance(now, datetime):
        return now.isoformat()
    return str(now or datetime.now().isoformat())


def _payload(value: dict | None) -> str:
    return json.dumps(value or {}, ensure_ascii=False, sort_keys=True)


def _metadata(raw: str | None) -> dict:
    try:
        return json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}


def encode_centroid(values) -> bytes:
    return np.asarray(list(values or ()), dtype=np.float32).tobytes()


def decode_centroid(raw: bytes | None) -> list[float]:
    if not raw:
        return []
    return np.frombuffer(raw, dtype=np.float32).tolist()


def _decode_neuron(row) -> dict | None:
    if row is None:
        return None
    neuron = dict(zip(NEURON_FIELDS, tuple(row)))
    neuron["metadata"] = _metadata(neuron.get("metadata"))
    neuron["centroid"] = decode_centroid(neuron.get("centroid"))
    return neuron


def _decode_synapse(row) -> dict:
    link = dict(zip(SYNAPSE_FIELDS, tuple(row)))
    link["metadata"] = _metadata(link.get("metadata"))
    return link


def upsert_neuron(neuron: dict, db_path: str | None = None) -> bool:
    neuron_id = str(neuron.get("neuron_id") or "").strip()
    if not neuron_id:
        return False
    now = datetime.now().isoformat()
    centroid = neuron.get("centroid")
    if isinstance(centroid, (list, tuple)):
        centroid = encode_centroid(centroid)
    with store.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO neurons
              (neuron_id, episode_id, title, summary, centroid, member_count,
               tier, status, heat, importance, created_at, updated_at, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(neuron_id) DO UPDATE SET
              episode_id=excluded.episode_id,
              title=excluded.title,
              summary=excluded.summary,
              centroid=excluded.centroid,
              member_count=excluded.member_count,
              tier=excluded.tier,
              status=excluded.status,
              heat=excluded.heat,
              importance=excluded.importance,
              updated_at=excluded.updated_at,
              metadata=excluded.metadata
            """,
            (
                neuron_id,
                str(neuron.get("episode_id") or "").strip() or None,
                str(neuron.get("title") or ""),
                str(neuron.get("summary") or ""),
                centroid,
                int(neuron.get("member_count") or 0),
                str(neuron.get("tier") or "active"),
                str(neuron.get("status") or "active"),
                float(neuron.get("heat") or 0.0),
                float(neuron.get("importance") or 0.0),
                str(neuron.get("created_at") or now),
                now,
                _payload(neuron.get("metadata")),
            ),
        )
    return True


def set_neuron_centroid(neuron_id: str, values, db_path: str | None = None) -> bool:
    if not neuron_id:
        return False
    with store.connect(db_path) as connection:
        cursor = connection.execute(
            "UPDATE neurons SET centroid=?, updated_at=? WHERE neuron_id=?",
            (encode_centroid(values), datetime.now().isoformat(), neuron_id),
        )
    return cursor.rowcount > 0


def ensure_neuron(neuron: dict, db_path: str | None = None) -> bool:
    neuron_id = str(neuron.get("neuron_id") or "").strip()
    if not neuron_id:
        return False
    now = datetime.now().isoformat()
    centroid = neuron.get("centroid")
    if isinstance(centroid, (list, tuple)):
        centroid = encode_centroid(centroid)
    with store.connect(db_path) as connection:
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO neurons
              (neuron_id, episode_id, title, summary, centroid, member_count,
               tier, status, heat, importance, created_at, updated_at, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                neuron_id,
                str(neuron.get("episode_id") or "").strip() or None,
                str(neuron.get("title") or ""),
                str(neuron.get("summary") or ""),
                centroid,
                int(neuron.get("member_count") or 0),
                str(neuron.get("tier") or "active"),
                str(neuron.get("status") or "active"),
                float(neuron.get("heat") or 0.0),
                float(neuron.get("importance") or 0.0),
                str(neuron.get("created_at") or now),
                now,
                _payload(neuron.get("metadata")),
            ),
        )
    return cursor.rowcount > 0


def membership_map(db_path: str | None = None) -> dict[str, str]:
    with store.connect(db_path) as connection:
        rows = connection.execute(
            "SELECT memory_id, neuron_id FROM neuron_members"
        ).fetchall()
    return {str(memory_id): str(neuron_id) for memory_id, neuron_id in rows}


def activation_ids(db_path: str | None = None) -> set[str]:
    with store.connect(db_path) as connection:
        rows = connection.execute("SELECT node_id FROM memory_activation").fetchall()
    return {str(row[0]) for row in rows}


def get_neuron(neuron_id: str, db_path: str | None = None) -> dict | None:
    with store.connect(db_path) as connection:
        row = connection.execute(
            f"SELECT {', '.join(NEURON_FIELDS)} FROM neurons WHERE neuron_id=?",
            (neuron_id,),
        ).fetchone()
    return _decode_neuron(row)


def list_neurons(
    *,
    tier: str = "",
    status: str = "",
    limit: int = 500,
    offset: int = 0,
    db_path: str | None = None,
) -> list[dict]:
    sql = f"SELECT {', '.join(NEURON_FIELDS)} FROM neurons WHERE 1=1"
    params: list[object] = []
    if tier:
        sql += " AND tier=?"
        params.append(tier)
    if status:
        sql += " AND status=?"
        params.append(status)
    sql += " ORDER BY updated_at DESC, neuron_id LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    with store.connect(db_path) as connection:
        rows = connection.execute(sql, params).fetchall()
    return [n for n in (_decode_neuron(row) for row in rows) if n]


def upsert_member(
    neuron_id: str,
    memory_id: str,
    *,
    layer: str = "hot",
    weight: float = 1.0,
    role: str = "member",
    joined_at: str = "",
    db_path: str | None = None,
) -> bool:
    if not neuron_id or not memory_id:
        return False
    with store.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO neuron_members
              (neuron_id, memory_id, layer, weight, role, joined_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(neuron_id, memory_id) DO UPDATE SET
              layer=excluded.layer,
              weight=excluded.weight,
              role=excluded.role
            """,
            (
                neuron_id,
                memory_id,
                layer,
                float(weight),
                role,
                joined_at or datetime.now().isoformat(),
            ),
        )
    return True


def remove_member(
    memory_id: str,
    *,
    neuron_id: str = "",
    db_path: str | None = None,
) -> int:
    sql = "DELETE FROM neuron_members WHERE memory_id=?"
    params: list[object] = [memory_id]
    if neuron_id:
        sql += " AND neuron_id=?"
        params.append(neuron_id)
    with store.connect(db_path) as connection:
        return connection.execute(sql, params).rowcount


_MEMBER_FIELDS = (
    "n.node_id, n.layer, n.status, n.title, n.type, n.source_file, "
    "n.created_at, n.metadata, m.layer, m.weight, m.role"
)


def _decode_member(row) -> dict:
    return {
        "node_id": row[0],
        "layer": row[1],
        "status": row[2],
        "title": row[3],
        "type": row[4],
        "source_file": row[5],
        "created_at": row[6],
        "metadata": _metadata(row[7]),
        "member_layer": row[8],
        "member_weight": _float(row[9], 1.0),
        "member_role": row[10],
    }


def members_of(
    neuron_id: str, *, limit: int = 200, db_path: str | None = None
) -> list[dict]:
    if not neuron_id:
        return []
    with store.connect(db_path) as connection:
        rows = connection.execute(
            f"""
            SELECT {_MEMBER_FIELDS}
            FROM neuron_members m
            JOIN memory_nodes n ON n.node_id=m.memory_id
            WHERE m.neuron_id=?
            ORDER BY m.weight DESC, n.node_id
            LIMIT ?
            """,
            (neuron_id, limit),
        ).fetchall()
    return [_decode_member(row) for row in rows]


def members_of_many(
    neuron_ids: list[str],
    *,
    limit: int = 5,
    db_path: str | None = None,
    readonly: bool = False,
) -> dict[str, list[dict]]:
    """One query for many neurons; avoids a connection per expanded neuron."""
    ids = sorted({str(value) for value in neuron_ids if value})
    out: dict[str, list[dict]] = {value: [] for value in ids}
    if not ids:
        return out
    marks = ",".join("?" * len(ids))
    with store.connect(db_path, readonly=readonly) as connection:
        rows = connection.execute(
            f"""
            SELECT * FROM (
              SELECT {_MEMBER_FIELDS}, m.neuron_id,
                     ROW_NUMBER() OVER (
                       PARTITION BY m.neuron_id ORDER BY m.weight DESC, n.node_id
                     ) AS _rank
              FROM neuron_members m
              JOIN memory_nodes n ON n.node_id=m.memory_id
              WHERE m.neuron_id IN ({marks})
            ) WHERE _rank<=?
            """,
            (*ids, limit),
        ).fetchall()
    for row in rows:
        out.setdefault(str(row[11]), []).append(_decode_member(row[:11]))
    return out


def neuron_of(memory_id: str, db_path: str | None = None) -> str:
    if not memory_id:
        return ""
    with store.connect(db_path) as connection:
        row = connection.execute(
            "SELECT neuron_id FROM neuron_members WHERE memory_id=? "
            "ORDER BY weight DESC, neuron_id LIMIT 1",
            (memory_id,),
        ).fetchone()
    return str(row[0]) if row else ""


def refresh_neuron_counts(neuron_id: str = "", db_path: str | None = None) -> int:
    with store.connect(db_path) as connection:
        if neuron_id:
            cursor = connection.execute(
                "UPDATE neurons SET member_count=("
                "SELECT COUNT(*) FROM neuron_members m WHERE m.neuron_id=neurons.neuron_id"
                ") WHERE neuron_id=?",
                (neuron_id,),
            )
            return cursor.rowcount
        cursor = connection.execute(
            "UPDATE neurons SET member_count=("
            "SELECT COUNT(*) FROM neuron_members m WHERE m.neuron_id=neurons.neuron_id)"
        )
        return cursor.rowcount


def _float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def upsert_synapse(link: dict, db_path: str | None = None) -> bool:
    left = str(link.get("from_neuron") or "").strip()
    right = str(link.get("to_neuron") or "").strip()
    if not left or not right or left == right:
        return False
    directed = bool(link.get("directed", False))
    if not directed and right < left:
        left, right = right, left
    weight = _float(link.get("weight"), 0.0)
    now = datetime.now().isoformat()
    with store.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO synapses
              (from_neuron, to_neuron, weight, base_weight, relation, directed,
               status, last_activated_at, activation_count, decay_rate,
               reinforce_rate, created_at, updated_at, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(from_neuron, to_neuron) DO UPDATE SET
              weight=MAX(synapses.weight, excluded.weight),
              base_weight=MAX(synapses.base_weight, excluded.base_weight),
              relation=excluded.relation,
              status=excluded.status,
              updated_at=excluded.updated_at,
              metadata=excluded.metadata
            """,
            (
                left,
                right,
                weight,
                _float(link.get("base_weight"), weight),
                str(link.get("relation") or ""),
                int(directed),
                str(link.get("status") or "active"),
                link.get("last_activated_at"),
                int(link.get("activation_count") or 0),
                _float(link.get("decay_rate"), 0.0),
                _float(link.get("reinforce_rate"), 0.0),
                str(link.get("created_at") or now),
                now,
                _payload(link.get("metadata")),
            ),
        )
    return True


def synapses_of(
    neuron_id: str,
    *,
    min_weight: float = 0.0,
    limit: int = 20,
    status: str = "active",
    db_path: str | None = None,
) -> list[dict]:
    if not neuron_id:
        return []
    sql = (
        f"SELECT {', '.join(SYNAPSE_FIELDS)} FROM synapses "
        "WHERE (from_neuron=? OR to_neuron=?) AND weight>=?"
    )
    params: list[object] = [neuron_id, neuron_id, min_weight]
    if status:
        sql += " AND status=?"
        params.append(status)
    sql += " ORDER BY weight DESC, synapse_id LIMIT ?"
    params.append(limit)
    with store.connect(db_path) as connection:
        rows = connection.execute(sql, params).fetchall()
    out = []
    for row in rows:
        link = _decode_synapse(row)
        link["other"] = (
            link["to_neuron"]
            if link["from_neuron"] == neuron_id
            else link["from_neuron"]
        )
        out.append(link)
    return out


def synapses_of_many(
    neuron_ids: list[str],
    *,
    min_weight: float = 0.0,
    limit: int = 8,
    db_path: str | None = None,
) -> dict[str, list[dict]]:
    """One query for many neurons; avoids a connection per frontier neuron."""
    ids = sorted({str(value) for value in neuron_ids if value})
    out: dict[str, list[dict]] = {value: [] for value in ids}
    if not ids:
        return out
    marks = ",".join("?" * len(ids))
    fields = ", ".join(SYNAPSE_FIELDS)
    with store.connect(db_path) as connection:
        rows = connection.execute(
            f"""
            SELECT * FROM (
              SELECT {fields}, from_neuron AS _neuron, to_neuron AS _other,
                     ROW_NUMBER() OVER (
                       PARTITION BY from_neuron ORDER BY weight DESC, synapse_id
                     ) AS _rank
              FROM synapses
              WHERE status='active' AND weight>=? AND from_neuron IN ({marks})
              UNION ALL
              SELECT {fields}, to_neuron AS _neuron, from_neuron AS _other,
                     ROW_NUMBER() OVER (
                       PARTITION BY to_neuron ORDER BY weight DESC, synapse_id
                     ) AS _rank
              FROM synapses
              WHERE status='active' AND weight>=? AND to_neuron IN ({marks})
            ) WHERE _rank<=?
            """,
            (min_weight, *ids, min_weight, *ids, limit),
        ).fetchall()
    for row in rows:
        link = _decode_synapse(row[: len(SYNAPSE_FIELDS)])
        link["other"] = str(row[len(SYNAPSE_FIELDS) + 1])
        out.setdefault(str(row[len(SYNAPSE_FIELDS)]), []).append(link)
    return out


def all_synapses(
    *,
    min_weight: float = 0.0,
    limit: int = 5000,
    status: str = "active",
    db_path: str | None = None,
    readonly: bool = False,
) -> list[dict]:
    sql = f"SELECT {', '.join(SYNAPSE_FIELDS)} FROM synapses WHERE weight>=?"
    params: list[object] = [min_weight]
    if status:
        sql += " AND status=?"
        params.append(status)
    sql += " ORDER BY weight DESC, synapse_id LIMIT ?"
    params.append(limit)
    with store.connect(db_path, readonly=readonly) as connection:
        rows = connection.execute(sql, params).fetchall()
    return [_decode_synapse(row) for row in rows]


def get_activation(node_id: str, db_path: str | None = None) -> dict | None:
    with store.connect(db_path) as connection:
        row = connection.execute(
            f"SELECT {', '.join(ACTIVATION_FIELDS)} FROM memory_activation "
            "WHERE node_id=?",
            (node_id,),
        ).fetchone()
    if row is None:
        return None
    activation = dict(zip(ACTIVATION_FIELDS, tuple(row)))
    activation["metadata"] = _metadata(activation.get("metadata"))
    return activation


def upsert_activation(node_id: str, patch: dict, db_path: str | None = None) -> bool:
    if not node_id:
        return False
    current = get_activation(node_id, db_path=db_path) or {
        "node_id": node_id,
        "strength": 0.0,
        "tier": "active",
        "last_activated_at": None,
        "activation_count": 0,
        "tier_changed_at": None,
        "purge_after": None,
        "metadata": {},
    }
    for key in ACTIVATION_PATCH_KEYS:
        if key in patch:
            current[key] = patch[key]
    with store.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO memory_activation
              (node_id, strength, tier, last_activated_at, activation_count,
               tier_changed_at, purge_after, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(node_id) DO UPDATE SET
              strength=excluded.strength,
              tier=excluded.tier,
              last_activated_at=excluded.last_activated_at,
              activation_count=excluded.activation_count,
              tier_changed_at=excluded.tier_changed_at,
              purge_after=excluded.purge_after,
              metadata=excluded.metadata
            """,
            (
                node_id,
                _float(current.get("strength"), 0.0),
                str(current.get("tier") or "active"),
                current.get("last_activated_at"),
                int(current.get("activation_count") or 0),
                current.get("tier_changed_at"),
                current.get("purge_after"),
                _payload(current.get("metadata")),
            ),
        )
    return True


def activations(node_ids: list[str], db_path: str | None = None) -> dict[str, dict]:
    ids = [node_id for node_id in dict.fromkeys(node_ids) if node_id]
    if not ids:
        return {}
    out: dict[str, dict] = {}
    with store.connect(db_path) as connection:
        for start in range(0, len(ids), 500):
            batch = ids[start : start + 500]
            placeholders = ",".join("?" for _ in batch)
            rows = connection.execute(
                f"SELECT {', '.join(ACTIVATION_FIELDS)} FROM memory_activation "
                f"WHERE node_id IN ({placeholders})",
                batch,
            ).fetchall()
            for row in rows:
                activation = dict(zip(ACTIVATION_FIELDS, tuple(row)))
                activation["metadata"] = _metadata(activation.get("metadata"))
                out[str(activation["node_id"])] = activation
    return out


def batch_set_tiers(
    updates: list[tuple[str, str, str | None, str]],
    db_path: str | None = None,
) -> int:
    if not updates:
        return 0
    rows = [
        (tier, purge_after, changed_at, node_id)
        for node_id, tier, purge_after, changed_at in updates
    ]
    with store.connect(db_path) as connection:
        connection.executemany(
            "UPDATE memory_activation SET tier=?, purge_after=?, tier_changed_at=? "
            "WHERE node_id=?",
            rows,
        )
    return len(rows)


def tier_counts(db_path: str | None = None) -> dict[str, int]:
    counts = {tier: 0 for tier in TIERS}
    with store.connect(db_path) as connection:
        rows = connection.execute(
            "SELECT tier, COUNT(*) FROM memory_activation GROUP BY tier"
        ).fetchall()
    for tier, value in rows:
        counts[str(tier)] = int(value)
    return counts


def stats(db_path: str | None = None) -> dict:
    with store.connect(db_path) as connection:
        neurons = int(connection.execute("SELECT COUNT(*) FROM neurons").fetchone()[0])
        synapses = int(
            connection.execute(
                "SELECT COUNT(*) FROM synapses WHERE status='active'"
            ).fetchone()[0]
        )
        tiers = {tier: 0 for tier in TIERS}
        for tier, value in connection.execute(
            "SELECT tier, COUNT(*) FROM memory_activation GROUP BY tier"
        ):
            tiers[str(tier)] = int(value)
        orphan_members = int(
            connection.execute(
                "SELECT COUNT(*) FROM neuron_members WHERE memory_id NOT IN "
                "(SELECT node_id FROM memory_nodes) OR neuron_id NOT IN "
                "(SELECT neuron_id FROM neurons)"
            ).fetchone()[0]
        )
        degrees = [
            int(row[0])
            for row in connection.execute(
                "SELECT COUNT(*) FROM ("
                "SELECT from_neuron AS neuron_id FROM synapses WHERE status='active' "
                "UNION ALL SELECT to_neuron FROM synapses WHERE status='active') "
                "GROUP BY neuron_id"
            )
        ]
    degrees.sort()
    p95 = degrees[max(0, int(len(degrees) * 0.95) - 1)] if degrees else 0
    return {
        "neurons": neurons,
        "synapses": synapses,
        "tiers": tiers,
        "orphan_members": orphan_members,
        "degree_p95": p95,
    }


def get_meta(key: str, db_path: str | None = None) -> str:
    with store.connect(db_path) as connection:
        row = connection.execute(
            "SELECT value FROM network_meta WHERE key=?", (key,)
        ).fetchone()
    return str(row[0]) if row else ""


def set_meta(key: str, value: str, db_path: str | None = None) -> bool:
    if not key:
        return False
    with store.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO network_meta (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
              value=excluded.value,
              updated_at=excluded.updated_at
            """,
            (key, value, datetime.now().isoformat()),
        )
    return True
