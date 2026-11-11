"""Tiered memory lifecycle: classify, archive, trash and physically purge."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from agent_gateway.core import config
from agent_gateway.core.log_utils import setup_logging
from agent_gateway.core.paths import gateway_cache
from agent_gateway.services.memory import network, network_build, store

logger = setup_logging("server")

ACTIVE_MIN_STRENGTH = 0.45
WARM_MIN_STRENGTH = 0.20
WEAK_IDLE_DAYS = 30
WEAK_IMPORTANCE_MAX = 3
ARCHIVE_IDLE_DAYS = 90


def _nodes(db_path: str | None = None) -> dict[str, dict]:
    nodes: dict[str, dict] = {}
    offset = 0
    while True:
        page = store.list_nodes(limit=2000, offset=offset, db_path=db_path)
        nodes.update({node["node_id"]: node for node in page})
        if len(page) < 2000:
            break
        offset += 2000
    return nodes


def _degrees(db_path: str | None = None) -> dict[str, int]:
    degrees: dict[str, int] = {}
    with store.connect(db_path) as connection:
        rows = connection.execute(
            "SELECT neuron_id, COUNT(*) FROM ("
            "SELECT from_neuron AS neuron_id FROM synapses WHERE status='active' "
            "UNION ALL SELECT to_neuron FROM synapses WHERE status='active') "
            "GROUP BY neuron_id"
        ).fetchall()
    for neuron_id, value in rows:
        degrees[str(neuron_id)] = int(value)
    return degrees


def _days_since(value: str, now: datetime) -> float:
    try:
        start = datetime.fromisoformat(str(value)).replace(tzinfo=None)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, (now.replace(tzinfo=None) - start).total_seconds() / 86400)


def _meta(node: dict) -> dict:
    return node.get("metadata") or {}


def _protected(node: dict) -> tuple[bool, int]:
    meta = _meta(node)
    importance = int(meta.get("importance") or 5)
    threshold = config.memory_protect_importance()
    protected = bool(meta.get("protected")) or (
        threshold is not None and importance >= threshold
    )
    return protected, importance


def classify_tiers(
    *,
    now: datetime | None = None,
    db_path: str | None = None,
    dry_run: bool = False,
) -> dict:
    now = now or datetime.now()
    archive_days = config.memory_neuron_archive_days()
    nodes = _nodes(db_path)
    membership = network.membership_map(db_path=db_path)
    degrees = _degrees(db_path)
    with store.connect(db_path) as connection:
        rows = connection.execute(
            "SELECT node_id, strength, tier, activation_count, last_activated_at "
            "FROM memory_activation"
        ).fetchall()
    counts = {"active": 0, "warm": 0, "archive": 0, "trash": 0, "changed": 0}
    updates: list[tuple[str, str, str | None, str]] = []
    for node_id, strength, current_tier, activation_count, last_active in rows:
        node = nodes.get(str(node_id))
        if node is None:
            continue
        protected, importance = _protected(node)
        meta = _meta(node)
        last = (
            str(last_active or "")
            or str(meta.get("last_accessed_at") or "")
            or str(node.get("created_at") or "")
        )
        idle = _days_since(last, now)
        degree = degrees.get(membership.get(str(node_id), ""), 0)
        weak = (
            degree == 0
            and int(activation_count or 0) == 0
            and importance <= WEAK_IMPORTANCE_MAX
            and idle >= WEAK_IDLE_DAYS
        )
        purge_after = None
        if protected:
            tier = "active"
        elif weak:
            tier = "archive"
            purge_after = (now + timedelta(days=archive_days)).isoformat()
        elif float(strength or 0.0) >= ACTIVE_MIN_STRENGTH or idle <= WEAK_IDLE_DAYS:
            tier = "active"
        elif float(strength or 0.0) >= WARM_MIN_STRENGTH:
            tier = "warm"
        elif idle >= ARCHIVE_IDLE_DAYS:
            tier = "archive"
            purge_after = (now + timedelta(days=archive_days)).isoformat()
        else:
            tier = "warm"
        counts[tier] += 1
        if tier != str(current_tier or ""):
            updates.append((str(node_id), tier, purge_after, now.isoformat()))
            counts["changed"] += 1
    if not dry_run:
        network.batch_set_tiers(updates, db_path=db_path)
    return counts


def mark_trashed(
    node_id: str, *, now: datetime | None = None, db_path: str | None = None
) -> bool:
    now = now or datetime.now()
    return network.upsert_activation(
        node_id,
        {
            "tier": "trash",
            "tier_changed_at": now.isoformat(),
            "purge_after": (
                now + timedelta(days=config.memory_neuron_trash_days())
            ).isoformat(),
        },
        db_path=db_path,
    )


def _hot():
    from agent_gateway.services.mcp.server_state import get_hot_memory

    return get_hot_memory()


def _qdrant():
    from agent_gateway.services.mcp.server_state import get_qdrant

    return get_qdrant()


def _log_purged(rows: list[dict]) -> None:
    if not rows:
        return
    path = gateway_cache() / "memory_network_purge.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def purge_expired(
    *,
    now: datetime | None = None,
    db_path: str | None = None,
    dry_run: bool = False,
    batch: int = 500,
) -> dict:
    now = now or datetime.now()
    with store.connect(db_path) as connection:
        rows = connection.execute(
            "SELECT a.node_id, a.tier, a.purge_after FROM memory_activation a "
            "WHERE a.tier IN ('archive','trash') AND a.purge_after IS NOT NULL "
            "ORDER BY a.purge_after"
        ).fetchall()
    eligible: list[tuple[str, str, str]] = []
    naive_now = now.replace(tzinfo=None)
    for node_id, tier, purge_after in rows:
        try:
            due = datetime.fromisoformat(str(purge_after)).replace(tzinfo=None)
        except (TypeError, ValueError):
            continue
        if due > naive_now:
            continue
        eligible.append((str(node_id), str(tier), str(purge_after)))
        if len(eligible) >= batch:
            break
    if dry_run:
        return {
            "eligible": len(eligible),
            "deleted": 0,
            "failed": 0,
            "neurons_removed": 0,
            "dry_run": True,
        }
    hot = _hot()
    qdrant = _qdrant()
    deleted = 0
    failed = 0
    logged: list[dict] = []
    for node_id, tier, purge_after in eligible:
        node = store.get_node(node_id, db_path=db_path)
        if node is None:
            with store.connect(db_path) as connection:
                connection.execute(
                    "DELETE FROM memory_activation WHERE node_id=?", (node_id,)
                )
            continue
        protected, _ = _protected(node)
        if protected:
            continue
        layer = str(node.get("layer") or "")
        point_id = str(_meta(node).get("point_id") or "")
        if layer == "hot" and hot is None:
            logger.warning(
                f"[memory-lifecycle] purge skipped, hot store absent: {node_id}"
            )
            failed += 1
            continue
        if layer == "cold" and (qdrant is None or not point_id):
            logger.warning(
                f"[memory-lifecycle] purge skipped, cold store absent: {node_id}"
            )
            failed += 1
            continue
        try:
            if layer == "hot":
                hot.delete(node_id)
            elif layer == "cold":
                from agent_gateway.services.mcp.server_constants import COLD_COLLECTION

                qdrant.delete(
                    collection_name=COLD_COLLECTION, points_selector=[point_id]
                )
            with store.connect(db_path) as connection:
                connection.execute(
                    "DELETE FROM memory_edges WHERE from_id=? OR to_id=?",
                    (node_id, node_id),
                )
                connection.execute(
                    "DELETE FROM neuron_members WHERE memory_id=?", (node_id,)
                )
                connection.execute(
                    "DELETE FROM memory_nodes WHERE node_id=?", (node_id,)
                )
                connection.execute(
                    "DELETE FROM memory_activation WHERE node_id=?", (node_id,)
                )
        except Exception as e:  # noqa: BLE001 - one failed delete must not stop the batch
            logger.warning(f"[memory-lifecycle] purge failed for {node_id}: {e}")
            failed += 1
            continue
        deleted += 1
        logged.append(
            {
                "deleted_at": now.isoformat(),
                "node_id": node_id,
                "tier": tier,
                "purge_after": purge_after,
            }
        )
    _log_purged(logged)
    reconciled = network_build.reconcile_network(db_path=db_path)
    return {
        "eligible": len(eligible),
        "deleted": deleted,
        "failed": failed,
        "neurons_removed": reconciled["empty_neurons"],
        "dry_run": False,
    }
