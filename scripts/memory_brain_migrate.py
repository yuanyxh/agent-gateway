#!/usr/bin/env python3
"""Migrate the memory graph to schema v3, or restore the newest v2 backup."""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from agent_gateway.services.memory import store


def _stamp() -> str:
    return f"{datetime.now():%Y%m%d%H%M%S}"


def _backup_path(db: Path, version: int) -> Path:
    return db.with_name(f"{db.stem}.v{version}-{_stamp()}.sqlite")


def _backup(source: sqlite3.Connection, target: Path) -> Path:
    with sqlite3.connect(target) as destination:
        source.backup(destination)
    return target


def _missing(connection: sqlite3.Connection) -> dict[str, list[str]]:
    tables = store.table_names(connection)
    out: dict[str, list[str]] = {}
    for table, columns in store.V3_COLUMNS.items():
        if table not in tables:
            continue
        existing = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        absent = [name for name, _ in columns if name not in existing]
        if absent:
            out[table] = absent
    return out


def _missing_tables(connection: sqlite3.Connection) -> list[str]:
    expected = {
        "entities",
        "entity_mentions",
        "activation_buffer",
        "replay_runs",
    }
    return sorted(expected - store.table_names(connection))


def _find_backup(db: Path) -> Path | None:
    candidates = sorted(db.parent.glob(f"{db.stem}.v[0-9]*-*.sqlite"))
    return candidates[-1] if candidates else None


def _rollback(db: Path) -> dict:
    backup = _find_backup(db)
    if backup is None:
        raise SystemExit(f"no v2 backup found for {db}")
    if db.exists():
        with sqlite3.connect(db) as source:
            safety = db.with_name(f"{db.stem}.pre-rollback-{_stamp()}.sqlite")
            _backup(source, safety)
    with sqlite3.connect(backup) as source, sqlite3.connect(db) as target:
        source.backup(target)
    return {"restored": str(backup), "bytes": db.stat().st_size}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(store.graph_db_path()))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--rollback", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--confirm", action="store_true")
    args = parser.parse_args()
    db = Path(args.db)
    if not db.is_file():
        raise SystemExit(f"database not found: {db}")
    if args.rollback:
        if not args.confirm:
            raise SystemExit("--rollback requires --confirm")
        print(json.dumps(_rollback(db), ensure_ascii=False))
        return 0
    with sqlite3.connect(db) as connection:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        missing_columns = _missing(connection)
        missing_tables = _missing_tables(connection)
        if args.dry_run:
            print(
                json.dumps(
                    {
                        "version": version,
                        "target": store.SCHEMA_VERSION,
                        "missing_columns": missing_columns,
                        "missing_tables": missing_tables,
                        "backup": str(_backup_path(db, version)),
                    },
                    ensure_ascii=False,
                )
            )
            return 0
        if version >= store.SCHEMA_VERSION and not missing_tables:
            print(json.dumps({"version": version, "changes": 0}))
            return 0
        if version >= store.SCHEMA_VERSION and missing_tables and not args.force:
            raise SystemExit("schema is v3 but tables are missing; rerun with --force")
        backup = _backup(connection, _backup_path(db, version))
    with store.connect(str(db)) as connection:
        missing_columns = _missing(connection)
        missing_tables = _missing_tables(connection)
        final = int(connection.execute("PRAGMA user_version").fetchone()[0])
    print(
        json.dumps(
            {
                "version": final,
                "backup": str(backup),
                "missing_columns": missing_columns,
                "missing_tables": missing_tables,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
