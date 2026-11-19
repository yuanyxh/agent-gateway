#!/usr/bin/env python3
"""Backfill metadata.project on active memory nodes from their source paths.

Only nodes whose metadata.project is empty are touched; a node with no
recognizable path keeps an empty project. Dry-run is read-only and prints what
would change. --apply takes a VACUUM INTO snapshot next to the database first.

Usage:
    python scripts/backfill_project.py --db <path> --dry-run
    python scripts/backfill_project.py --db <path> --apply
"""

import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from agent_gateway.services.memory.project_infer import infer_project

SAMPLE_SIZE = 10


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="memory_graph.sqlite path")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="report only (default)")
    mode.add_argument("--apply", action="store_true", help="backup then update")
    return parser.parse_args()


def connect(db_path: str, *, readonly: bool) -> sqlite3.Connection:
    if readonly:
        return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10.0)
    connection = sqlite3.connect(db_path, timeout=10.0)
    connection.execute("PRAGMA busy_timeout=5000")
    return connection


def source_paths(row) -> list[str]:
    _node_id, _layer, source_file, raw_metadata = row
    try:
        metadata = json.loads(raw_metadata or "{}")
    except (TypeError, ValueError):
        metadata = {}
    files = metadata.get("source_files")
    paths = [str(item) for item in files if item] if isinstance(files, list) else []
    if source_file:
        paths.append(str(source_file))
    return paths


def plan_updates(rows) -> tuple[list[tuple[str, dict, str]], dict]:
    updates: list[tuple[str, dict, str]] = []
    stats = {"found": 0, "inferred": 0, "no_match": 0, "no_paths": 0}
    for row in rows:
        metadata = json.loads(row[3] or "{}")
        if str(metadata.get("project") or "").strip():
            continue
        stats["found"] += 1
        paths = source_paths(row)
        if not paths:
            stats["no_paths"] += 1
            stats["no_match"] += 1
            continue
        project = infer_project(paths)
        if not project:
            stats["no_match"] += 1
            continue
        stats["inferred"] += 1
        updates.append((row[0], metadata, project))
    return updates, stats


def main() -> int:
    args = parse_args()
    db_path = str(Path(args.db).expanduser())
    if not Path(db_path).exists():
        print(json.dumps({"error": f"database not found: {db_path}"}))
        return 2

    backup = ""
    if args.apply:
        backup = f"{db_path}.bak-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        backup_connection = connect(db_path, readonly=False)
        try:
            backup_connection.execute("VACUUM INTO ?", (backup,))
        finally:
            backup_connection.close()

    connection = connect(db_path, readonly=not args.apply)
    try:
        rows = connection.execute(
            "SELECT node_id, layer, source_file, metadata FROM memory_nodes "
            "WHERE status='active' AND COALESCE(json_extract(metadata, '$.project'), '')=''"
        ).fetchall()
        updates, stats = plan_updates(rows)
        sample = [
            {"node_id": node_id, "project": project}
            for node_id, _metadata, project in updates[:SAMPLE_SIZE]
        ]
        if args.apply and updates:
            connection.executemany(
                "UPDATE memory_nodes SET metadata=? WHERE node_id=?",
                [
                    (
                        json.dumps(
                            {**metadata, "project": project}, ensure_ascii=False
                        ),
                        node_id,
                    )
                    for node_id, metadata, project in updates
                ],
            )
            connection.commit()
    finally:
        connection.close()

    print(
        json.dumps(
            {
                "db": db_path,
                "mode": "apply" if args.apply else "dry-run",
                "backup": backup,
                **stats,
                "applied": stats["inferred"] if args.apply else 0,
                "sample": sample,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
