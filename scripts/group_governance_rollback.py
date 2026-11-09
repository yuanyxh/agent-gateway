#!/usr/bin/env python3
"""Roll back episode ids written by the daily group governance pass."""

from __future__ import annotations

# ruff: noqa: E402 - cache isolation must run before agent_gateway imports

try:
    from _eval_env import isolate_cache
except ModuleNotFoundError:
    from scripts._eval_env import isolate_cache

isolate_cache(__file__)

import argparse
import shutil
from datetime import datetime
from pathlib import Path

from agent_gateway.services.memory import store

PAGE_SIZE = 5000


def _all_nodes(db_path: str) -> list[dict]:
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


def _sync_mem0(member_id: str, episode: str) -> bool:
    """Restore the mem0 payload; recall reads it instead of the graph."""
    try:
        from agent_gateway.services.mcp import server_memory

        hot = server_memory.get_hot_memory()
        if not hot:
            return False
        row = hot.get(member_id) or {}
        meta = dict(row.get("metadata") or {})
        meta["episode_id"] = episode
        meta.pop("episode_prev", None)
        meta.pop("episode_source", None)
        meta.pop("governance_run", None)
        hot.update(member_id, metadata=meta)
        return True
    except Exception:  # noqa: BLE001 - reported as a batch failure below
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="memory graph sqlite path")
    parser.add_argument(
        "--since", default="", help="only runs at or after this ISO time"
    )
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    updates: list[tuple[str, dict]] = []
    for node in _all_nodes(args.db):
        meta = node.get("metadata") or {}
        previous = str(meta.get("episode_prev") or "")
        run_at = str(meta.get("governance_run") or "")
        if not previous or (args.since and run_at < args.since):
            continue
        updates.append(
            (
                str(node.get("node_id") or ""),
                {
                    "episode_id": previous,
                    "episode_prev": None,
                    "episode_source": None,
                    "governance_run": None,
                },
            )
        )

    print({"candidates": len(updates)})
    if not args.apply or not updates:
        return 0

    failed = [
        member_id
        for member_id, patch in updates
        if not _sync_mem0(member_id, str(patch["episode_id"]))
    ]
    if failed:
        print(
            {
                "error": "mem0 sync failed; stop the MCP process and rerun",
                "failed": len(failed),
            }
        )
        return 3

    db = Path(args.db)
    backup = db.with_name(f"{db.name}.bak-{datetime.now():%Y%m%d%H%M%S}")
    shutil.copy2(db, backup)
    written = 0
    for start in range(0, len(updates), 500):
        written += store.batch_update_metadata(
            updates[start : start + 500], db_path=str(db)
        )
    print({"restored": written, "backup": str(backup)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
