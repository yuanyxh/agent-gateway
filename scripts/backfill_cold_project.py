#!/usr/bin/env python3
"""Fill cold-layer project from source_file paths; idempotent and reversible."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from qdrant_client import QdrantClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_gateway.services.memory.project_infer import infer_project  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--qdrant", required=True)
    parser.add_argument("--collection", default="knowledge_base_jinazh768")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup", default="")
    args = parser.parse_args()

    client = QdrantClient(path=args.qdrant)
    updated: list[dict] = []
    offset = None
    while True:
        points, offset = client.scroll(
            collection_name=args.collection,
            limit=500,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        for point in points:
            payload = point.payload or {}
            if payload.get("project"):
                continue
            project = infer_project([str(payload.get("source_file") or "")])
            if project:
                updated.append(
                    {
                        "id": str(point.id),
                        "project": project,
                        "previous": payload.get("project"),
                    }
                )
        if offset is None:
            break

    print(f"updateable={len(updated)} apply={args.apply}")
    if args.backup and updated:
        Path(args.backup).write_text(
            json.dumps(updated, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"backup={args.backup}")
    if not args.apply:
        return 0

    by_project: dict[str, list[str]] = {}
    for row in updated:
        by_project.setdefault(row["project"], []).append(row["id"])
    for project, ids in by_project.items():
        client.set_payload(
            collection_name=args.collection,
            payload={"project": project},
            points=ids,
        )
        print(f"set project={project} points={len(ids)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
