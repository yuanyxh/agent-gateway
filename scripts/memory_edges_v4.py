#!/usr/bin/env python3
"""Move free-text predicates out of memory_edges.type into a column."""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from agent_gateway.services.memory import store


def _counts(connection: sqlite3.Connection) -> dict[str, int]:
    controlled = ",".join(f"'{kind}'" for kind in store.CONTROLLED_EDGE_TYPES)
    facts = connection.execute(
        f"SELECT COUNT(*) FROM memory_edges WHERE type NOT IN ({controlled})"
    ).fetchone()[0]
    structural = connection.execute(
        f"SELECT COUNT(*) FROM memory_edges WHERE type IN ({controlled})"
    ).fetchone()[0]
    groups = connection.execute(
        "SELECT COUNT(*) FROM (SELECT content_hash FROM memory_nodes "
        "WHERE status='active' GROUP BY content_hash HAVING COUNT(*) > 1)"
    ).fetchone()[0]
    missing = 0
    for (raw,) in connection.execute("SELECT metadata FROM memory_nodes"):
        try:
            meta = json.loads(raw or "{}")
        except ValueError:
            meta = {}
        if "importance" not in meta:
            missing += 1
    return {
        "facts": facts,
        "structural": structural,
        "duplicate_groups": groups,
        "importance_missing": missing,
    }


def _backfill_importance(connection: sqlite3.Connection) -> int:
    changed = 0
    rows = connection.execute("SELECT node_id, metadata FROM memory_nodes").fetchall()
    for node_id, raw in rows:
        try:
            meta = json.loads(raw or "{}")
        except ValueError:
            continue
        if "importance" in meta:
            continue
        meta["importance"] = 5
        connection.execute(
            "UPDATE memory_nodes SET metadata=? WHERE node_id=?",
            (json.dumps(meta, ensure_ascii=False), node_id),
        )
        changed += 1
    return changed


def _dedupe(connection: sqlite3.Connection) -> int:
    changed = 0
    groups = connection.execute(
        "SELECT content_hash FROM memory_nodes WHERE status='active' "
        "GROUP BY content_hash HAVING COUNT(*) > 1"
    ).fetchall()
    for (content_hash,) in groups:
        rows = connection.execute(
            "SELECT node_id FROM memory_nodes "
            "WHERE content_hash=? AND status='active' ORDER BY updated_at DESC",
            (content_hash,),
        ).fetchall()
        for (node_id,) in rows[1:]:
            connection.execute(
                "UPDATE memory_nodes SET status='superseded' WHERE node_id=?",
                (node_id,),
            )
            changed += 1
    return changed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(store.graph_db_path()))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not args.dry_run and not args.apply:
        parser.error("pass --dry-run or --apply")

    if args.dry_run:
        with sqlite3.connect(args.db) as connection:
            counts = _counts(connection)
        print(" ".join(f"{key}={value}" for key, value in counts.items()))
        return 0

    source = Path(args.db)
    backup = source.with_name(
        f"{source.stem}.pre-v4-{datetime.now():%Y%m%d%H%M%S}.sqlite"
    )
    with sqlite3.connect(args.db) as connection:
        connection.execute("VACUUM INTO ?", (str(backup),))

    with store.connect(args.db) as connection:
        importance = _backfill_importance(connection)
        deduped = _dedupe(connection)
        counts = _counts(connection)

    summary = " ".join(f"{key}={value}" for key, value in counts.items())
    print(f"backup={backup} importance={importance} deduped={deduped} {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
