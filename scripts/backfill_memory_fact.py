#!/usr/bin/env python3
"""Backfill graph metadata.fact from the hot-layer Qdrant payload."""

from __future__ import annotations

import argparse
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

from qdrant_client import QdrantClient

from agent_gateway.services.memory import store
from agent_gateway.services.memory.link import fact_from_data

PAGE_SIZE = 5000


def _pick_shard(source: str, collection: str) -> Path:
    """Active data may live in a tenant shard such as `<collection>_<tenant>`."""
    root = Path(source)
    candidates = [root / collection, *sorted(root.glob(f"{collection}_*"))]

    def size(path: Path) -> int:
        storage = path / "collection" / collection / "storage.sqlite"
        return storage.stat().st_size if storage.is_file() else 0

    return max(candidates, key=size)


def _copy_qdrant(source: str, collection: str) -> str:
    """Qdrant local holds an exclusive lock; read a copy so the gateway keeps running."""
    root = Path(tempfile.mkdtemp(prefix="qdrant-copy-"))
    shard = _pick_shard(source, collection)
    target = root / "collection" / collection
    target.mkdir(parents=True)
    shutil.copy2(shard / "collection" / collection / "storage.sqlite", target)
    meta = shard / "meta.json"
    if meta.is_file():
        shutil.copy2(meta, root / "meta.json")
    return str(root)


def _scroll_payloads(qdrant_path: str, collection: str) -> dict[str, str]:
    client = QdrantClient(path=qdrant_path)
    out: dict[str, str] = {}
    offset = None
    try:
        while True:
            points, offset = client.scroll(
                collection,
                limit=1000,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            for point in points:
                data = str((point.payload or {}).get("data") or "")
                if data:
                    out[str(point.id)] = fact_from_data(data)
            if offset is None:
                break
    finally:
        client.close()
    return out


def _all_hot_nodes(db_path: str) -> list[dict]:
    nodes: list[dict] = []
    offset = 0
    while True:
        page = store.list_nodes(
            layer="hot",
            limit=PAGE_SIZE,
            offset=offset,
            readonly=True,
            db_path=db_path,
        )
        nodes.extend(page)
        if len(page) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return nodes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qdrant", required=True, help="hot-layer Qdrant directory")
    parser.add_argument("--collection", default="mem0")
    parser.add_argument("--db", required=True, help="memory graph sqlite path")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    copy = _copy_qdrant(args.qdrant, args.collection)
    payloads = _scroll_payloads(copy, args.collection)
    nodes = _all_hot_nodes(args.db)
    updates: list[tuple[str, dict]] = []
    matched = same = 0
    for node in nodes:
        node_id = str(node.get("node_id") or "")
        data = payloads.get(node_id)
        if not data:
            continue
        matched += 1
        existing = str((node.get("metadata") or {}).get("fact") or "")
        if existing == data:
            same += 1
            continue
        updates.append((node_id, {"fact": data}))

    print(
        {
            "qdrant_copy": copy,
            "qdrant_points": len(payloads),
            "graph_nodes": len(nodes),
            "matched": matched,
            "already_complete": same,
            "to_update": len(updates),
            "missing_in_qdrant": len(nodes) - matched,
        }
    )
    if not args.apply or not updates:
        return 0

    db_path = Path(args.db)
    backup = db_path.with_name(f"{db_path.name}.bak-{datetime.now():%Y%m%d%H%M%S}")
    shutil.copy2(db_path, backup)
    written = 0
    for start in range(0, len(updates), 500):
        written += store.batch_update_metadata(
            updates[start : start + 500], db_path=str(db_path)
        )
    print({"updated": written, "backup": str(backup)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
