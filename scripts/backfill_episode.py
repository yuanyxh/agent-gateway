#!/usr/bin/env python3
"""Backfill episode_id over historical hot-layer memories.

Replays the live write path in created_at order: top-1 cosine at or above the
dedupe threshold means the fact would not have been stored twice, a score at or
above the inherit threshold joins the target group while it still has room, and
anything else opens a new group. Keys are written with set_payload, which keeps
the payload flat exactly like mem0's own writes.

Qdrant local mode takes an exclusive file lock, so run this against a copy and
swap the directory while the MCP server is stopped.

Usage:
    python scripts/backfill_episode.py --path <mem0 dir> --dry-run
    python scripts/backfill_episode.py --path <mem0 dir> --manifest out.json
    python scripts/backfill_episode.py --path <mem0 dir> --revert out.json
"""

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime

import numpy as np
from qdrant_client import QdrantClient

COLLECTION = "mem0"
DEDUPE_SIMILARITY = 0.97
BATCH_SIZE = 200


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", required=True, help="Qdrant mem0 directory")
    parser.add_argument("--threshold", type=float, default=0.80)
    parser.add_argument("--cap", type=int, default=8)
    parser.add_argument("--manifest", default="backfill_manifest.json")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--revert", metavar="MANIFEST")
    return parser.parse_args()


def dense(raw):
    if isinstance(raw, dict):
        raw = raw.get("") or raw.get("dense")
    return raw


def load_points(client):
    points, offset = [], None
    while True:
        batch, offset = client.scroll(
            COLLECTION,
            limit=1000,
            offset=offset,
            with_payload=True,
            with_vectors=True,
        )
        points.extend(batch)
        if offset is None:
            return points


def is_active(payload):
    return (payload.get("status") or "active") == "active" and not payload.get(
        "archived"
    )


def build_records(points):
    """Active points without a group key, oldest first; already-keyed rows are skipped."""
    records = []
    for point in points:
        payload = point.payload or {}
        if not is_active(payload) or (payload.get("episode_id") or "").strip():
            continue
        vector = dense(point.vector)
        if not vector:
            continue
        array = np.asarray(vector, dtype=np.float32)
        norm = np.linalg.norm(array)
        if norm == 0:
            continue
        records.append(
            {
                "id": point.id,
                "vec": array / norm,
                "created": payload.get("created_at", ""),
            }
        )
    records.sort(key=lambda r: r["created"])
    return records


def assign_groups(records, threshold, cap):
    """Replay the write path. Duplicate rows share a group but do not fill the cap."""
    n = len(records)
    vecs = np.stack([r["vec"] for r in records]) if n else np.zeros((0, 1))
    group = np.full(n, -1, dtype=int)
    counted = np.zeros(n, dtype=bool)
    sizes = Counter()
    next_id = 0
    for i in range(n):
        if i == 0:
            group[i], sizes[next_id], counted[i] = next_id, 1, True
            next_id += 1
            continue
        sims = vecs[:i] @ vecs[i]
        sims[~counted[:i]] = -9.0
        k = min(3, i)
        idx = np.argpartition(-sims, k - 1)[:k]
        idx = idx[np.argsort(-sims[idx])]
        best, score = int(idx[0]), float(sims[idx[0]])
        target = int(group[best])
        if score >= DEDUPE_SIMILARITY:
            group[i] = target
        elif score >= threshold and sizes[target] < cap:
            group[i], counted[i] = target, True
            sizes[target] += 1
        else:
            group[i], sizes[next_id], counted[i] = next_id, 1, True
            next_id += 1
    return group, counted, sizes


def group_name(first_created, seq):
    day = (first_created or "").replace("-", "")[:8]
    if not day:
        day = datetime.now().strftime("%Y%m%d")
    return f"ep_{day}_{seq:04d}"


def write_groups(client, records, group, dry_run, manifest_path):
    by_group = defaultdict(list)
    for i in range(len(records)):
        by_group[int(group[i])].append(i)

    manifest = {}
    for gid, idxs in by_group.items():
        first = min(records[i]["created"] for i in idxs)
        name = group_name(first, gid)
        ids = [records[i]["id"] for i in idxs]
        for i in idxs:
            manifest[str(records[i]["id"])] = name
        if dry_run:
            continue
        for start in range(0, len(ids), BATCH_SIZE):
            client.set_payload(
                COLLECTION,
                payload={"episode_id": name},
                points=ids[start : start + BATCH_SIZE],
            )
    with open(manifest_path, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2)
    return manifest


def revert(client, manifest_path, dry_run):
    with open(manifest_path, encoding="utf-8") as handle:
        ids = list(json.load(handle))
    if not dry_run:
        for start in range(0, len(ids), BATCH_SIZE):
            client.set_payload(
                COLLECTION,
                payload={"episode_id": ""},
                points=ids[start : start + BATCH_SIZE],
            )
    return len(ids)


def summarize(records, counted, sizes):
    counts = np.array(list(sizes.values())) if sizes else np.array([0])
    return {
        "backfilled": len(records),
        "counted": int(counted.sum()),
        "groups": int(len(counts)),
        "single_ratio": round(float((counts == 1).mean()), 3),
        "max_size": int(counts.max()),
        "groups_ge_10": int((counts >= 10).sum()),
    }


def main():
    args = parse_args()
    client = QdrantClient(path=args.path)
    try:
        if args.revert:
            total = revert(client, args.revert, args.dry_run)
            print(json.dumps({"reverted": total, "dry_run": args.dry_run}))
            return 0
        records = build_records(load_points(client))
        group, counted, sizes = assign_groups(records, args.threshold, args.cap)
        write_groups(client, records, group, args.dry_run, args.manifest)
        stats = summarize(records, counted, sizes)
        stats.update({"dry_run": args.dry_run, "manifest": args.manifest})
        print(json.dumps(stats, ensure_ascii=False))
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
