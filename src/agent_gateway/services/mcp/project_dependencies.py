"""MCP project dependencies tool."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import threading

import numpy as np

from agent_gateway.core.db import conn as dbconn
from agent_gateway.core.log_utils import setup_logging
from agent_gateway.core.synonyms import load_synonyms
from agent_gateway.core.tool_defaults import arg_default
from agent_gateway.services.mcp.server_embed import _get_shared_embedder, embed_query

from agent_gateway.services.mcp.search_constants import (
    DEFAULT_PROJECT_DEPENDENCIES_DIRECTION,
    DEFAULT_PROJECT_DEPENDENCIES_LIMIT,
    logger,
)


def _dependency_row(row: sqlite3.Row) -> dict:
    return {
        "fromPath": row["from_path"],
        "toPath": row["to_path"],
        "rawTarget": row["raw_target"],
        "kind": row["kind"],
        "line": row["line"],
        "confidence": row["confidence"],
        "resolver": row["resolver"],
    }


def _dependency_group(rows: list[sqlite3.Row], limit: int) -> dict:
    resolved, external, unresolved = [], [], []
    for row in rows[:limit]:
        item = _dependency_row(row)
        if row["to_path"]:
            resolved.append(item)
        elif row["is_external"]:
            external.append(item)
        else:
            unresolved.append(item)
    return {"resolved": resolved, "external": external, "unresolved": unresolved}


def _dependency_counts(conn: sqlite3.Connection, where: str, args: tuple) -> dict:
    rows = conn.execute(
        "SELECT CASE WHEN to_path IS NOT NULL THEN 'resolved' "
        "WHEN is_external THEN 'external' ELSE 'unresolved' END AS grp, "
        f"COUNT(*) AS n FROM imports WHERE {where} GROUP BY grp",
        args,
    ).fetchall()
    counts = {"resolved": 0, "external": 0, "unresolved": 0}
    for row in rows:
        counts[row["grp"]] = row["n"]
    return counts


def _empty_dependency_groups() -> dict:
    return {
        "resolved": [],
        "external": [],
        "unresolved": [],
    }


def tool_project_dependencies(
    db_path: str,
    path: str,
    direction: str = DEFAULT_PROJECT_DEPENDENCIES_DIRECTION,
    limit: int = DEFAULT_PROJECT_DEPENDENCIES_LIMIT,
) -> dict:
    """One-hop file imports (outgoing) and importers (incoming)."""
    if not os.path.isfile(db_path):
        return {"error": f"Database not found: {db_path}"}
    if direction not in ("outgoing", "incoming", "both"):
        direction = DEFAULT_PROJECT_DEPENDENCIES_DIRECTION
    limit = max(1, limit)

    empty = {
        "path": path,
        "isDirectory": False,
        "direction": direction,
        "outgoing": _empty_dependency_groups(),
        "incoming": _empty_dependency_groups(),
        "counts": {
            "outgoingResolved": 0,
            "outgoingExternal": 0,
            "outgoingUnresolved": 0,
            "incomingResolved": 0,
            "incomingExternal": 0,
            "incomingUnresolved": 0,
        },
        "truncated": False,
    }

    with dbconn(db_path) as conn:
        conn.row_factory = sqlite3.Row
        try:
            has_imports = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='imports'"
            ).fetchone()
        except sqlite3.DatabaseError as e:
            return {"error": str(e)}
        if has_imports is None:
            empty["degraded"] = True
            empty["hint"] = (
                "imports table not found; run `utility init` to rebuild the project graph"
            )
            return empty

        is_dir = (
            conn.execute("SELECT 1 FROM directories WHERE path = ?", (path,)).fetchone()
            is not None
        )
        empty["isDirectory"] = is_dir

        if is_dir:
            prefix = path.rstrip("/") + "/"
            out_where, out_args = "from_path GLOB ?", (prefix + "*",)
            in_where, in_args = "to_path GLOB ?", (prefix + "*",)
        else:
            out_where, out_args = "from_path = ?", (path,)
            in_where, in_args = "to_path = ?", (path,)

        select = (
            "SELECT from_path, raw_target, to_path, is_external, kind, line, "
            "confidence, resolver FROM imports"
        )
        out_rows = []
        in_rows = []
        out_counts = {"resolved": 0, "external": 0, "unresolved": 0}
        in_counts = {"resolved": 0, "external": 0, "unresolved": 0}
        if direction in ("outgoing", "both"):
            out_rows = conn.execute(
                f"{select} WHERE {out_where} ORDER BY from_path, line, raw_target LIMIT ?",
                (*out_args, limit + 1),
            ).fetchall()
            out_counts = _dependency_counts(conn, out_where, out_args)
        if direction in ("incoming", "both"):
            in_rows = conn.execute(
                f"{select} WHERE {in_where} ORDER BY from_path, line, raw_target LIMIT ?",
                (*in_args, limit + 1),
            ).fetchall()
            in_counts = _dependency_counts(conn, in_where, in_args)

    outgoing = _dependency_group(out_rows, limit)
    incoming = _dependency_group(in_rows, limit)
    counts = {
        "outgoingResolved": out_counts["resolved"],
        "outgoingExternal": out_counts["external"],
        "outgoingUnresolved": out_counts["unresolved"],
        "incomingResolved": in_counts["resolved"],
        "incomingExternal": in_counts["external"],
        "incomingUnresolved": in_counts["unresolved"],
    }
    return {
        "path": path,
        "isDirectory": is_dir,
        "direction": direction,
        "outgoing": outgoing,
        "incoming": incoming,
        "counts": counts,
        "truncated": len(out_rows) > limit or len(in_rows) > limit,
    }
