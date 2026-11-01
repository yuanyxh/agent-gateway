"""Mirror memory-store writes into the node/edge graph.

The graph is a side index: a failure here is logged and never blocks the memory
write itself, but it must be loud, because a silent failure shows up later as a
chain that cannot be traced.
"""

from __future__ import annotations

from datetime import datetime

from agent_gateway.core.log_utils import setup_logging
from agent_gateway.services.memory import graph, store

logger = setup_logging("server")

_TITLE_CHARS = 120


def _title(content: str) -> str:
    line = " ".join(str(content or "").split())
    return line[:_TITLE_CHARS]


def _int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def record_hot_memory(
    memory_id: str,
    content: str,
    metadata: dict,
    *,
    supersedes: tuple[str, ...] = (),
    created_at: str = "",
    db_path: str | None = None,
) -> None:
    """Register a hot memory plus its supersede and episode edges."""
    if not memory_id:
        return
    try:
        meta = dict(metadata or {})
        episode_id = str(meta.get("episode_id") or "").strip()
        source_files = [str(item) for item in meta.get("source_files") or [] if item]
        node_meta = {
            "point_id": memory_id,
            "episode_id": episode_id,
            "revision": meta.get("revision", 1),
            "supersedes": [sid for sid in supersedes if sid],
            "type": str(meta.get("type") or ""),
            "source_files": source_files,
            "source_url": str(meta.get("source_url") or ""),
            "generated_by": str(meta.get("generated_by") or ""),
            # Upsert replaces metadata wholesale, so anything not listed here is
            # dropped on the next write.
            "level": _int(meta.get("level"), 0),
            "importance": _int(meta.get("importance"), 5),
            "protected": bool(meta.get("protected", False)),
        }
        store.upsert_node(
            memory_id,
            layer="hot",
            status=str(meta.get("status") or "active"),
            content_hash_value=store.content_hash(content),
            type=str(meta.get("type") or ""),
            title=_title(content),
            source_file=source_files[0] if source_files else "",
            created_at=created_at or str(meta.get("created_at") or ""),
            metadata=node_meta,
            db_path=db_path,
        )
        edges = [
            (memory_id, sid, "supersedes") for sid in supersedes if sid != memory_id
        ]
        if episode_id:
            members = store.find_nodes_by_metadata(
                "episode_id",
                episode_id,
                layer="hot",
                exclude=memory_id,
                db_path=db_path,
            )
            edges.extend(
                (memory_id, member["node_id"], "episode") for member in members
            )
        graph.add_edges(edges, db_path=db_path)
    except Exception as e:  # noqa: BLE001 - graph is a side index
        logger.warning(f"[graph] hot node write failed for {memory_id}: {e}")


def record_hot_status(
    memory_ids: list[str],
    status: str,
    *,
    superseded_by: str = "",
    db_path: str | None = None,
) -> None:
    """Propagate a lifecycle change into the node table and the version chain."""
    if not memory_ids:
        return
    try:
        edges = []
        for memory_id in memory_ids:
            store.set_status(memory_id, status, db_path=db_path)
            if status == "superseded" and superseded_by and superseded_by != memory_id:
                edges.append((superseded_by, memory_id, "supersedes"))
        graph.add_edges(edges, db_path=db_path)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[graph] status write failed for {memory_ids[:3]}: {e}")


def record_cold_memory(
    point_id: str,
    content: str,
    *,
    source_file: str = "",
    source_memory_ids: list[str] | None = None,
    episode_id: str = "",
    created_at: str = "",
    db_path: str | None = None,
) -> str:
    """Register a cold point and link it back to the hot memories it came from."""
    body_hash = store.content_hash(content)
    node_id = store.cold_node_id(source_file, body_hash)
    try:
        # A legacy uuid4 point and the deterministic point can both exist for the
        # same body; keep the old id reachable so an id handed out earlier still
        # resolves through memory_get.
        previous = (store.get_node(node_id, db_path=db_path) or {}).get(
            "metadata"
        ) or {}
        legacy = list(previous.get("legacy_ids") or [])
        previous_point = str(previous.get("point_id") or "")
        if (
            previous_point
            and previous_point != point_id
            and previous_point not in legacy
        ):
            legacy.append(previous_point)
        store.upsert_node(
            node_id,
            layer="cold",
            status="active",
            content_hash_value=body_hash,
            title=_title(content),
            source_file=source_file,
            created_at=created_at or datetime.now().isoformat(),
            metadata={
                "point_id": point_id,
                "legacy_ids": legacy,
                "episode_id": episode_id,
                "source_memory_ids": list(source_memory_ids or []),
            },
            db_path=db_path,
        )
        edges = [
            (node_id, source, "derived_from")
            for source in source_memory_ids or []
            if source
        ]
        if episode_id:
            members = store.find_nodes_by_metadata(
                "episode_id", episode_id, exclude=node_id, db_path=db_path
            )
            edges.extend((node_id, member["node_id"], "episode") for member in members)
        graph.add_edges(edges, db_path=db_path)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[graph] cold node write failed for {point_id}: {e}")
    return node_id


def record_dispute(
    left_id: str,
    right_id: str,
    reason: str = "",
    *,
    db_path: str | None = None,
) -> None:
    """One edge per disputed pair; the pair is symmetric, so it is stored once."""
    if not left_id or not right_id or left_id == right_id:
        return
    first, second = sorted((left_id, right_id))
    try:
        graph.add_edge(
            first,
            second,
            "disputes",
            metadata={"reason": reason},
            db_path=db_path,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[graph] dispute edge failed {left_id} <-> {right_id}: {e}")


def record_episode_node(
    node_id: str, episode_id: str, db_path: str | None = None
) -> None:
    """Join an existing node to its episode group without rewriting its metadata."""
    if not node_id or not episode_id:
        return
    try:
        members = store.find_nodes_by_metadata(
            "episode_id", episode_id, exclude=node_id, db_path=db_path
        )
        graph.add_edges(
            [(node_id, member["node_id"], "episode") for member in members],
            db_path=db_path,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[graph] episode link failed for {node_id}: {e}")
