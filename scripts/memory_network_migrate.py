#!/usr/bin/env python3
"""Migrate the memory graph into neurons and reconcile membership."""

from __future__ import annotations

import argparse
import json

from agent_gateway.services.memory import network_build, store


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(store.graph_db_path()))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--reconcile", action="store_true")
    args = parser.parse_args()
    if args.reconcile:
        result = network_build.reconcile_network(db_path=args.db)
    else:
        result = network_build.migrate_network(db_path=args.db, apply=args.apply)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
