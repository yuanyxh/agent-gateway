#!/usr/bin/env python3
"""Disable, export, or drop the memory-network tables."""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

import yaml

from agent_gateway.core.paths import gateway_home
from agent_gateway.services.memory import store

TABLES = (
    "neurons",
    "neuron_members",
    "synapses",
    "memory_activation",
    "network_meta",
    "entities",
    "entity_mentions",
    "activation_buffer",
    "replay_runs",
)


def _disable(config: Path) -> dict:
    data = yaml.safe_load(config.read_text(encoding="utf-8")) or {}
    memory = data.setdefault("memory", {})
    neuron = memory.setdefault("neuron", {})
    neuron["enabled"] = False
    neuron["maintenance"] = False
    backup = config.with_name(config.name + f".bak-{datetime.now():%Y%m%d%H%M%S}")
    shutil.copy2(config, backup)
    config.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    return {"config": str(config), "backup": str(backup)}


def _encode(value):
    if isinstance(value, bytes):
        return {"__bytes__": base64.b64encode(value).decode("ascii")}
    return value


def _decode(value):
    if isinstance(value, dict) and set(value) == {"__bytes__"}:
        return base64.b64decode(value["__bytes__"])
    return value


def _existing_tables(db: str) -> set[str]:
    path = Path(db)
    if not path.is_file():
        return set()
    try:
        connection = sqlite3.connect(path)
        try:
            return {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        finally:
            connection.close()
    except sqlite3.Error:  # a corrupt file is reported by the export itself
        return set()


def _export(db: str, target: Path) -> dict:
    payload: dict = {"schema_version": store.SCHEMA_VERSION}
    missing: list[str] = []
    with store.connect(db) as connection:
        available = store.table_names(connection)
        for table in TABLES:
            if table not in available:
                missing.append(table)
                continue
            rows = connection.execute(f"SELECT * FROM {table}").fetchall()
            columns = [
                item[1] for item in connection.execute(f"PRAGMA table_info({table})")
            ]
            payload[table] = [
                {key: _encode(value) for key, value in zip(columns, tuple(row))}
                for row in rows
            ]
    payload["missing_tables"] = missing
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    counts = {k: len(v) for k, v in payload.items() if isinstance(v, list)}
    return {
        "path": str(target),
        "tables": counts,
        "missing_tables": missing,
        "schema_version": store.SCHEMA_VERSION,
    }


def _import(db: str, source: Path) -> dict:
    payload = json.loads(source.read_text(encoding="utf-8"))
    restored = {}
    with store.connect(db) as connection:
        connection.executescript(store.SCHEMA)
        available = store.table_names(connection)
        for table in TABLES:
            rows = payload.get(table) or []
            if table not in available:
                restored[table] = 0
                continue
            connection.execute(f"DELETE FROM {table}")
            if not rows:
                restored[table] = 0
                continue
            columns = {
                item[1] for item in connection.execute(f"PRAGMA table_info({table})")
            }
            groups: dict[tuple[str, ...], list[dict]] = {}
            for row in rows:
                keys = tuple(sorted(key for key in row if key in columns))
                if keys:
                    groups.setdefault(keys, []).append(row)
            for keys, group in groups.items():
                marks = ",".join("?" * len(keys))
                connection.executemany(
                    f"INSERT OR REPLACE INTO {table} ({', '.join(keys)}) "
                    f"VALUES ({marks})",
                    [tuple(_decode(row.get(key)) for key in keys) for row in group],
                )
            restored[table] = len(rows)
        if "activation_buffer" in available:
            connection.execute("DELETE FROM activation_buffer")
    return {
        "path": str(source),
        "tables": restored,
        "schema_version": payload.get("schema_version"),
    }


def _drop(db: str) -> dict:
    dropped = []
    with store.connect(db) as connection:
        for table in TABLES:
            connection.execute(f"DROP TABLE IF EXISTS {table}")
            dropped.append(table)
        connection.execute("PRAGMA user_version=0")
    return {"dropped": dropped}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(store.graph_db_path()))
    parser.add_argument(
        "--config",
        default=os.environ.get("GATEWAY_CONFIG")
        or str(gateway_home() / "agent-gateway.yaml"),
    )
    parser.add_argument("--disable", action="store_true")
    parser.add_argument("--export-network", default="")
    parser.add_argument("--import-network", default="")
    parser.add_argument("--drop-network", action="store_true")
    parser.add_argument("--confirm", action="store_true")
    args = parser.parse_args()
    result = {}
    if args.disable:
        result["disable"] = _disable(Path(args.config))
    if args.export_network:
        result["export"] = _export(args.db, Path(args.export_network))
    if args.import_network:
        result["import"] = _import(args.db, Path(args.import_network))
    if args.drop_network:
        if not args.confirm or not args.export_network:
            raise SystemExit("--drop-network requires --export-network and --confirm")
        if not args.disable:
            raise SystemExit(
                "--drop-network requires --disable to avoid an immediate rebuild"
            )
        result["drop"] = _drop(args.db)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
