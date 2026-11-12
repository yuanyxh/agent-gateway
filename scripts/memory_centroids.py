#!/usr/bin/env python3
"""Recompute neuron centroids with the configured embedder (one vector space)."""

import argparse
import json
import sqlite3
from datetime import datetime

import numpy as np

from agent_gateway.services.memory import network, network_build, store
from agent_gateway.services.mcp import server_embed


def _text(item):
    fact = str((item.get("metadata") or {}).get("fact") or "").strip()
    return fact or str(item.get("title") or "").strip()


def recompute(db_path, *, batch=64, dry_run=False):
    groups = network_build._group_nodes(db_path)
    texts = {}
    for members in groups.values():
        for item in members:
            text = _text(item)
            if text:
                texts[item["node_id"]] = text
    node_ids = list(texts)
    vectors = {}
    for start in range(0, len(node_ids), batch):
        chunk = node_ids[start : start + batch]
        embedded = server_embed.embed_texts([texts[node] for node in chunk])
        for node, vector in zip(chunk, embedded):
            if len(vector):
                vectors[node] = np.asarray(vector, dtype=np.float32)
        print(
            json.dumps({"embedded": len(vectors), "total": len(node_ids)}), flush=True
        )
    if not vectors:
        raise SystemExit("embedder unavailable: no vectors produced")
    centroids = {}
    for neuron_id, members in groups.items():
        totals = [
            vectors[item["node_id"]] for item in members if item["node_id"] in vectors
        ]
        if not totals:
            continue
        center = np.mean(np.stack(totals), axis=0)
        norm = float(np.linalg.norm(center))
        if norm > 0:
            center = center / norm
        centroids[neuron_id] = center.astype(np.float32)
    summary = {
        "neurons": len(groups),
        "centroids": len(centroids),
        "dims": sorted({int(vec.shape[0]) for vec in centroids.values()}),
        "embedded": len(vectors),
    }
    if dry_run:
        return summary
    backup = f"{db_path}.pre-centroids-{datetime.now():%Y%m%d%H%M%S%f}"
    with store.connect(db_path) as connection:
        connection.execute("VACUUM INTO ?", (backup,))
    check = sqlite3.connect(f"file:{backup}?mode=ro", uri=True)
    try:
        tables = check.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
        ).fetchone()[0]
    finally:
        check.close()
    if tables == 0:
        raise SystemExit(f"backup verification failed: {backup}")
    now = datetime.now().isoformat()
    rows = [
        (network.encode_centroid(vector), now, neuron_id)
        for neuron_id, vector in centroids.items()
    ]
    with store.connect(db_path) as connection:
        connection.executemany(
            "UPDATE neurons SET centroid=?, updated_at=? WHERE neuron_id=?", rows
        )
    summary["backup"] = backup
    return summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(store.graph_db_path()))
    parser.add_argument("--batch", type=int, default=64)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    print(json.dumps(recompute(args.db, batch=args.batch, dry_run=args.dry_run)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
