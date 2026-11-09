#!/usr/bin/env python3
"""Backfill the memory graph from the hot and cold Qdrant payloads.

Nodes are upserted by their stable ids and edges by their unique
(from, to, type), so the script can be re-run at any time. Qdrant local mode
takes an exclusive file lock: point it at a copy, or stop the MCP server first.

Usage:
    PYTHONPATH=src python scripts/migrate_memory_graph.py --dry-run
    PYTHONPATH=src python scripts/migrate_memory_graph.py --db /tmp/graph.sqlite
"""

from __future__ import annotations

# ruff: noqa: E402 - cache isolation must run before agent_gateway imports

import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

try:
    from _eval_env import isolate_cache  # noqa: E402
except ModuleNotFoundError:
    from scripts._eval_env import isolate_cache  # noqa: E402

isolate_cache(__file__)

from qdrant_client import QdrantClient  # noqa: E402

from agent_gateway.services.memory import graph, store  # noqa: E402
from agent_gateway.services.retrieval.config import index_name, model_tag  # noqa: E402

DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--qdrant",
        default=os.environ.get("QDRANT_PATH", ""),
        help="Qdrant root (default: $QDRANT_PATH)",
    )
    parser.add_argument(
        "--db", default=str(store.graph_db_path()), help="Target graph database"
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--batch-size", type=int, default=1000)
    return parser.parse_args()


def scroll_all(client: QdrantClient, collection: str, batch: int) -> list:
    points, offset = [], None
    while True:
        chunk, offset = client.scroll(
            collection,
            limit=batch,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        points.extend(chunk)
        if offset is None:
            return points


def open_mem0(qdrant_root: str, batch: int) -> tuple[QdrantClient, list]:
    tag = model_tag(os.environ.get("EMBEDDER_MODEL", DEFAULT_MODEL))
    path = os.path.join(qdrant_root, index_name("mem0", tag))
    client = QdrantClient(path=path)
    names = [c.name for c in client.get_collections().collections]
    if "mem0" in names:
        collection = "mem0"
    elif len(names) == 1:
        collection = names[0]
    else:
        client.close()
        raise RuntimeError(f"cannot pick a mem0 collection from {names}")
    return client, scroll_all(client, collection, batch)


def open_cold(qdrant_root: str, batch: int) -> tuple[QdrantClient | None, list]:
    tag = model_tag(os.environ.get("EMBEDDER_MODEL", DEFAULT_MODEL))
    collection = index_name("knowledge_base", tag)
    client = QdrantClient(path=qdrant_root)
    names = [c.name for c in client.get_collections().collections]
    if collection not in names:
        client.close()
        return None, []
    return client, scroll_all(client, collection, batch)


def _meta(payload: dict) -> dict:
    return payload.get("metadata") or {}


def hot_entries(points: list) -> tuple[list[dict], list[tuple]]:
    nodes, edges = [], []
    for point in points:
        payload = point.payload or {}
        meta = _meta(payload)
        content = str(payload.get("data") or payload.get("memory") or "")
        node_id = str(point.id)
        episode = str(payload.get("episode_id") or meta.get("episode_id") or "")
        supersedes = payload.get("supersedes") or meta.get("supersedes") or []
        if isinstance(supersedes, str):
            supersedes = [supersedes]
        superseded_by = str(
            payload.get("superseded_by") or meta.get("superseded_by") or ""
        )
        nodes.append(
            {
                "node_id": node_id,
                "layer": "hot",
                "status": str(payload.get("status") or meta.get("status") or "active"),
                "content_hash_value": store.content_hash(content),
                "title": " ".join(content.split())[:120],
                "created_at": str(payload.get("created_at") or ""),
                "metadata": {
                    "point_id": node_id,
                    "episode_id": episode,
                    "revision": payload.get("revision") or meta.get("revision", 1),
                    "supersedes": list(supersedes),
                },
            }
        )
        edges.extend(
            (node_id, str(sid), "supersedes")
            for sid in supersedes
            if sid and str(sid) != node_id
        )
        if superseded_by and superseded_by != node_id:
            edges.append((superseded_by, node_id, "supersedes"))
    return nodes, edges


def cold_entries(points: list) -> tuple[list[dict], list[tuple], list[str]]:
    nodes, edges = [], []
    for point in points:
        payload = point.payload or {}
        content = str(payload.get("content") or "")
        source_file = str(payload.get("source_file") or "")
        point_id = str(point.id)
        body_hash = store.content_hash(content)
        node_id = store.cold_node_id(source_file, body_hash)
        sources = payload.get("source_memory_ids") or []
        episode = str(payload.get("episode_id") or "")
        nodes.append(
            {
                "node_id": node_id,
                "layer": "cold",
                "status": "archived" if payload.get("archived") else "active",
                "content_hash_value": body_hash,
                "title": " ".join(content.split())[:120],
                "source_file": source_file,
                "created_at": str(payload.get("created_at") or ""),
                "metadata": {
                    "point_id": point_id,
                    "legacy_id": point_id,
                    "episode_id": episode,
                    "source_memory_ids": list(sources),
                },
            }
        )
        edges.extend(
            (node_id, str(source), "derived_from") for source in sources if source
        )
    return nodes, edges, [node["node_id"] for node in nodes]


def episode_edges(nodes: list[dict]) -> list[tuple]:
    """Star per episode group: one hub keeps the edge count linear."""
    groups: dict[str, list[str]] = defaultdict(list)
    for node in nodes:
        episode = str((node.get("metadata") or {}).get("episode_id") or "")
        if episode:
            groups[episode].append(node["node_id"])
    edges = []
    for members in groups.values():
        hub = members[0]
        edges.extend(
            (hub, member, "episode") for member in members[1:] if member != hub
        )
    return edges


def main() -> int:
    args = parse_args()
    if not args.qdrant:
        print("QDRANT_PATH is required (or pass --qdrant)", file=sys.stderr)
        return 2

    mem0_client, hot_points = open_mem0(args.qdrant, args.batch_size)
    try:
        hot_nodes, hot_edges = hot_entries(hot_points)
    finally:
        mem0_client.close()

    cold_client, cold_points = open_cold(args.qdrant, args.batch_size)
    try:
        cold_nodes, cold_edges, _ = cold_entries(cold_points)
    finally:
        if cold_client is not None:
            cold_client.close()

    nodes = hot_nodes + cold_nodes
    edges = hot_edges + cold_edges + episode_edges(nodes)
    stats = {
        "hot_points": len(hot_points),
        "cold_points": len(cold_points),
        "nodes": len(nodes),
        "supersedes_edges": len(hot_edges),
        "derived_from_edges": len(cold_edges),
        "episode_edges": len(edges) - len(hot_edges) - len(cold_edges),
        "dry_run": args.dry_run,
        "db": args.db,
    }
    if not args.dry_run:
        store.upsert_nodes(nodes, db_path=args.db)
        graph.add_edges(edges, db_path=args.db)
        stats["db_nodes"] = store.count_nodes(db_path=args.db)
        stats["db_edges"] = graph.count_edges(db_path=args.db)
    print(json.dumps(stats, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
