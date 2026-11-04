"""MCP project graph tools: symbol lookup and impact traversal.

Both tools take a project root and read `<root>/.agent-gateway/graph.sqlite`
(or a legacy `.utility/graph.sqlite`). Results carry provenance (file, line,
resolver, confidence).
"""

from __future__ import annotations

import os
import secrets
import sqlite3
import urllib.parse
from contextlib import contextmanager
from datetime import datetime

from agent_gateway.core import project_paths
from agent_gateway.core.tool_defaults import arg_default
from agent_gateway.services.graph.constants import EXTENSIONS, TYPE_KINDS

LOOKUP_DEFAULT_LIMIT = arg_default("project_lookup", "limit")
IMPACT_DEFAULT_LIMIT = arg_default("project_impact", "limit")
IMPACT_DEFAULT_DEPTH = arg_default("project_impact", "depth")

DIRECTIONS = ("callers", "callees", "both")
MAX_LIMIT = 500
MAX_DEPTH = 10


def _graph_db_path(project_path: str) -> str:
    return str(project_paths.readable_graph_db(project_path))


#: Edge kinds the graph stores; anything else is a known blind spot.
COVERAGE = {
    "edges": ["call", "type_ref", "extends", "implements", "import"],
    "not_covered": ["dynamic_reference", "multi_hop_re_export"],
}


def _trace(prefix: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d")
    return f"{prefix}_{stamp}_{secrets.token_hex(4)}"


@contextmanager
def _read(db_path: str):
    uri = "file:" + urllib.parse.quote(os.path.abspath(db_path)) + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=5.0)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("BEGIN")
        yield connection
    finally:
        connection.close()


_PATH_EXTENSIONS = tuple(EXTENSIONS)


def _looks_like_path(query: str) -> bool:
    return "/" in query or query.lower().endswith(_PATH_EXTENSIONS)


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(int(value), high))


def _built_at(connection) -> str | None:
    try:
        row = connection.execute(
            "SELECT value FROM meta WHERE key='built_at'"
        ).fetchone()
    except sqlite3.Error:
        return None
    return row["value"] if row else None


def _caller_context(connection, to_key: str) -> tuple[int, list[dict]]:
    count_row = connection.execute(
        "SELECT COUNT(DISTINCT path) AS total FROM edges "
        "WHERE to_key=? AND status='ok'",
        (to_key,),
    ).fetchone()
    top_rows = connection.execute(
        "SELECT e.path, e.from_key, e.start_line, e.kind, e.resolver, "
        "s.name AS from_name "
        "FROM edges e LEFT JOIN symbols s ON e.from_key='sym:'||s.sym_key "
        "WHERE e.to_key=? AND e.status='ok' "
        "ORDER BY e.path, e.start_line LIMIT 3",
        (to_key,),
    ).fetchall()
    top = [
        {
            "file": row["path"],
            "symbol": row["from_name"],
            "line": row["start_line"],
            "kind": row["kind"],
            "weak": row["resolver"] == "global_unique",
        }
        for row in top_rows
    ]
    return int(count_row["total"]), top


def _symbol_result(connection, row) -> dict:
    count, top = _caller_context(connection, f"sym:{row['sym_key']}")
    return {
        "name": row["name"],
        "kind": row["kind"],
        "file": row["path"],
        "startLine": row["start_line"],
        "endLine": row["end_line"],
        "callerCount": count,
        "topCallers": top,
        "resolver": "treesitter",
    }


def _file_result(connection, path: str) -> dict:
    count, top = _caller_context(connection, f"file:{path}")
    return {
        "name": os.path.basename(path),
        "kind": "file",
        "file": path,
        "startLine": 0,
        "endLine": 0,
        "callerCount": count,
        "topCallers": top,
        "resolver": "filesystem",
    }


def _overview(connection) -> dict:
    languages = {
        row[0]: row[1]
        for row in connection.execute(
            "SELECT lang, COUNT(*) FROM files GROUP BY lang ORDER BY COUNT(*) DESC"
        )
    }
    edge_kinds = {
        row[0]: row[1]
        for row in connection.execute(
            "SELECT kind, COUNT(*) FROM edges WHERE status = 'ok' GROUP BY kind"
        )
    }
    return {
        "files": connection.execute("SELECT COUNT(*) FROM files").fetchone()[0],
        "symbols": connection.execute("SELECT COUNT(*) FROM symbols").fetchone()[0],
        "edges": connection.execute(
            "SELECT COUNT(*) FROM edges WHERE status = 'ok'"
        ).fetchone()[0],
        "languages": languages,
        "edgeKinds": edge_kinds,
        "builtAt": _built_at(connection),
    }


def _query_symbols(connection, query: str, kind: str, file_path: str, limit: int):
    clauses = ["name = ?"]
    params: list = [query]
    if kind:
        clauses.append("kind = ?")
        params.append(kind)
    if file_path:
        clauses.append("path LIKE ? ESCAPE '\\'")
        params.append(f"%{_escape_like(file_path)}%")
    rows = connection.execute(
        "SELECT sym_key,path,name,kind,signature,start_line,end_line "
        "FROM symbols WHERE " + " AND ".join(clauses) + " "
        "ORDER BY path, start_line LIMIT ?",
        params + [limit],
    ).fetchall()
    if rows:
        return rows

    clauses = ["name LIKE ? ESCAPE '\\'"]
    params = [f"%{_escape_like(query)}%"]
    if kind:
        clauses.append("kind = ?")
        params.append(kind)
    if file_path:
        clauses.append("path LIKE ? ESCAPE '\\'")
        params.append(f"%{_escape_like(file_path)}%")
    return connection.execute(
        "SELECT sym_key,path,name,kind,signature,start_line,end_line "
        "FROM symbols WHERE " + " AND ".join(clauses) + " "
        "ORDER BY path, start_line LIMIT ?",
        params + [limit],
    ).fetchall()


def _query_files(connection, query: str, file_path: str, limit: int):
    clauses = ["path LIKE ? ESCAPE '\\'"]
    params: list = [f"%{_escape_like(query)}%"]
    if file_path:
        clauses.append("path LIKE ? ESCAPE '\\'")
        params.append(f"%{_escape_like(file_path)}%")
    return connection.execute(
        "SELECT path FROM files WHERE " + " AND ".join(clauses) + " "
        "ORDER BY path LIMIT ?",
        params + [limit],
    ).fetchall()


def tool_project_lookup(
    project_path: str,
    query: str,
    kind: str = "",
    file_path: str = "",
    limit: int = LOOKUP_DEFAULT_LIMIT,
) -> dict:
    trace_id = _trace("gl")
    project_path = (project_path or "").strip()
    query = (query or "").strip()
    if not os.path.isdir(project_path):
        return {
            "status": "error",
            "query": query,
            "error": f"project directory not found: {project_path}",
            "trace_id": trace_id,
        }
    db_path = _graph_db_path(project_path)
    if not os.path.isfile(db_path):
        return {
            "status": "error",
            "query": query,
            "error": f"graph database not found: {db_path}",
            "trace_id": trace_id,
        }
    if not query:
        try:
            with _read(db_path) as connection:
                overview = _overview(connection)
        except sqlite3.Error as exc:
            return {
                "status": "error",
                "query": "",
                "error": str(exc),
                "trace_id": trace_id,
            }
        return {
            "status": "ok",
            "query": "",
            "overview": overview,
            "trace_id": trace_id,
        }

    limit = _clamp(limit, 1, MAX_LIMIT)
    results: list[dict] = []
    try:
        with _read(db_path) as connection:
            if _looks_like_path(query):
                file_rows = _query_files(connection, query, file_path, limit)
                results = [_file_result(connection, row["path"]) for row in file_rows]
            if not results:
                search_query = query
                if "." in query and not _looks_like_path(query):
                    owner, member = query.rsplit(".", 1)
                    if owner.isidentifier() and member.isidentifier():
                        placeholders = ",".join("?" * len(TYPE_KINDS))
                        owner_row = connection.execute(
                            "SELECT 1 FROM symbols WHERE name=? "
                            f"AND kind IN ({placeholders}) LIMIT 1",
                            (owner, *sorted(TYPE_KINDS)),
                        ).fetchone()
                        if owner_row is not None:
                            search_query = member
                rows = _query_symbols(connection, search_query, kind, file_path, limit)
                results = [_symbol_result(connection, row) for row in rows]
            if not results:
                file_rows = _query_files(connection, query, file_path, limit)
                results = [_file_result(connection, row["path"]) for row in file_rows]
    except sqlite3.Error as exc:
        return {
            "status": "error",
            "query": query,
            "error": str(exc),
            "trace_id": trace_id,
        }

    return {
        "status": "ok" if results else "no_match",
        "query": query,
        "results": results[:limit],
        "trace_id": trace_id,
    }


def _resolve_target(connection, target: str):
    text = (target or "").strip()
    if not text:
        return None
    if text.startswith("file:"):
        path = text[5:]
        row = connection.execute(
            "SELECT path FROM files WHERE path=? LIMIT 1", (path,)
        ).fetchone()
        return (
            (f"file:{row['path']}", os.path.basename(row["path"]), row["path"])
            if row is not None
            else None
        )

    def symbol_by_name(name: str):
        return connection.execute(
            "SELECT s.sym_key, s.path, s.name, "
            "(SELECT COUNT(*) FROM edges e "
            " WHERE e.to_key='sym:'||s.sym_key AND e.status='ok') AS incoming "
            "FROM symbols s WHERE s.name=? "
            "ORDER BY incoming DESC, s.path ASC, s.start_line ASC LIMIT 1",
            (name,),
        ).fetchone()

    def file_exact(path: str):
        return connection.execute(
            "SELECT path FROM files WHERE path=? LIMIT 1", (path,)
        ).fetchone()

    if _looks_like_path(text):
        row = file_exact(text)
        if row is None:
            row = connection.execute(
                "SELECT path FROM files WHERE path LIKE ? ESCAPE '\\' "
                "ORDER BY path LIMIT 1",
                (f"%{_escape_like(text)}%",),
            ).fetchone()
        if row is not None:
            return f"file:{row['path']}", os.path.basename(row["path"]), row["path"]

    if "." in text and "/" not in text and not _looks_like_path(text):
        owner, member = text.rsplit(".", 1)
        if owner.isidentifier() and member.isidentifier():
            placeholders = ",".join("?" * len(TYPE_KINDS))
            row = connection.execute(
                "SELECT s.sym_key, s.path, s.name, "
                "(SELECT COUNT(*) FROM edges e "
                " WHERE e.to_key='sym:'||s.sym_key AND e.status='ok') AS incoming "
                "FROM symbols s WHERE s.name=? AND EXISTS ("
                " SELECT 1 FROM symbols o WHERE o.path=s.path AND o.name=? "
                f" AND o.kind IN ({placeholders}) "
                " AND o.start_line <= s.start_line AND s.end_line <= o.end_line) "
                "ORDER BY incoming DESC, s.path ASC, s.start_line ASC LIMIT 1",
                (member, owner, *sorted(TYPE_KINDS)),
            ).fetchone()
            if row is not None:
                return f"sym:{row['sym_key']}", row["name"], row["path"]

    row = symbol_by_name(text)
    if row is not None:
        return f"sym:{row['sym_key']}", row["name"], row["path"]

    row = file_exact(text)
    if row is not None:
        return f"file:{row['path']}", os.path.basename(row["path"]), row["path"]

    row = connection.execute(
        "SELECT s.sym_key, s.path, s.name, "
        "(SELECT COUNT(*) FROM edges e "
        " WHERE e.to_key='sym:'||s.sym_key AND e.status='ok') AS incoming "
        "FROM symbols s WHERE s.name LIKE ? ESCAPE '\\' "
        "ORDER BY incoming DESC, s.path ASC, s.start_line ASC LIMIT 1",
        (f"%{_escape_like(text)}%",),
    ).fetchone()
    if row is not None:
        return f"sym:{row['sym_key']}", row["name"], row["path"]

    row = connection.execute(
        "SELECT path FROM files WHERE path LIKE ? ESCAPE '\\' ORDER BY path LIMIT 1",
        (f"%{_escape_like(text)}%",),
    ).fetchone()
    if row is not None:
        return f"file:{row['path']}", os.path.basename(row["path"]), row["path"]
    return None


def _node_info(
    connection, key: str, cache: dict
) -> tuple[str | None, str | None, int | None]:
    if key in cache:
        return cache[key]
    if key.startswith("sym:"):
        row = connection.execute(
            "SELECT path, name, start_line FROM symbols WHERE sym_key=?",
            (key[4:],),
        ).fetchone()
        info = (
            (row["path"], row["name"], row["start_line"])
            if row is not None
            else (None, None, None)
        )
    elif key.startswith("file:"):
        info = (key[5:], os.path.basename(key[5:]), None)
    else:
        info = (None, None, None)
    cache[key] = info
    return info


def _prefetch_symbols(connection, keys: set[str], cache: dict) -> None:
    missing = [key[4:] for key in keys if key.startswith("sym:") and key not in cache]
    for offset in range(0, len(missing), 500):
        chunk = missing[offset : offset + 500]
        placeholders = ",".join("?" * len(chunk))
        rows = connection.execute(
            f"SELECT sym_key,path,name,start_line FROM symbols "
            f"WHERE sym_key IN ({placeholders})",
            tuple(chunk),
        ).fetchall()
        found = {
            row["sym_key"]: (row["path"], row["name"], row["start_line"])
            for row in rows
        }
        for sym_key in chunk:
            cache["sym:" + sym_key] = found.get(sym_key, (None, None, None))


def _collect(
    connection,
    start_key: str,
    direction: str,
    depth: int,
    include_stale: bool,
    max_items: int,
    cache: dict,
) -> tuple[list[dict], set[int]]:
    entries: list[dict] = []
    stale_ids: set[int] = set()
    visited = {start_key}
    frontier = [start_key]
    column = "to_key" if direction == "callers" else "from_key"
    for hop in range(1, depth + 1):
        if not frontier:
            break
        placeholders = ",".join("?" * len(frontier))
        rows = connection.execute(
            "SELECT id,from_key,to_key,kind,path,start_line,end_line,"
            f"confidence,resolver,status FROM edges "
            f"WHERE {column} IN ({placeholders})",
            tuple(frontier),
        ).fetchall()
        discovered: list[tuple[dict, str]] = []
        for row in rows:
            if row["status"] != "ok":
                stale_ids.add(row["id"])
                if not include_stale:
                    continue
            other = row["from_key"] if direction == "callers" else row["to_key"]
            if other in visited:
                continue
            visited.add(other)
            discovered.append((dict(row), other))
        _prefetch_symbols(connection, {other for _, other in discovered}, cache)
        for row, other in discovered:
            path, symbol, symbol_line = _node_info(connection, other, cache)
            line = (
                symbol_line
                if direction == "callees" and symbol_line is not None
                else row["start_line"]
            )
            item = {
                "file": path or row["path"],
                "symbol": symbol or "",
                "line": line,
                "kind": row["kind"],
                "status": row["status"],
                "hop": hop,
            }
            entries.append(
                {
                    "item": item,
                    "resolver": row["resolver"],
                    "confidence": row["confidence"],
                    "direction": direction,
                }
            )
            if len(entries) >= max_items:
                return entries, stale_ids
        frontier = [other for _, other in discovered]
    return entries, stale_ids


def _entries_to_groups(entries: list[dict], limit: int) -> tuple[list[dict], bool, int]:
    dedup: dict[tuple, dict] = {}
    for entry in entries:
        item = entry["item"]
        key = (
            item["file"],
            item["symbol"],
            item["line"],
            item["kind"],
            entry["direction"],
        )
        dedup.setdefault(key, entry)
    ordered = sorted(
        dedup.values(),
        key=lambda entry: (
            entry["item"].get("hop", 1),
            -entry["confidence"],
            entry["resolver"],
            entry["item"]["file"],
            entry["item"]["line"] or 0,
        ),
    )
    truncated = len(ordered) > limit
    buckets: dict[tuple[str, float], list[dict]] = {}
    for entry in ordered[:limit]:
        buckets.setdefault((entry["resolver"], entry["confidence"]), []).append(
            entry["item"]
        )
    groups = [
        {
            "resolver": resolver,
            "confidence": confidence,
            "weak": resolver == "global_unique",
            "items": items,
        }
        for (resolver, confidence), items in sorted(
            buckets.items(), key=lambda pair: (-pair[0][1], pair[0][0])
        )
    ]
    return groups, truncated, len(ordered)


def tool_project_impact(
    project_path: str,
    target: str,
    direction: str = "callers",
    depth: int = IMPACT_DEFAULT_DEPTH,
    limit: int = IMPACT_DEFAULT_LIMIT,
    include_stale: bool = False,
) -> dict:
    trace_id = _trace("gi")
    project_path = (project_path or "").strip()
    target = (target or "").strip()
    direction = direction if direction in DIRECTIONS else "callers"
    depth = _clamp(depth, 1, MAX_DEPTH)
    limit = _clamp(limit, 1, MAX_LIMIT)
    empty_target = {"name": target, "file": ""}
    if not target:
        return {
            "status": "error",
            "target": empty_target,
            "direction": direction,
            "error": "target must not be empty",
            "trace_id": trace_id,
        }
    if not os.path.isdir(project_path):
        return {
            "status": "error",
            "target": empty_target,
            "direction": direction,
            "error": f"project directory not found: {project_path}",
            "trace_id": trace_id,
        }
    db_path = _graph_db_path(project_path)
    if not os.path.isfile(db_path):
        return {
            "status": "error",
            "target": empty_target,
            "direction": direction,
            "error": f"graph database not found: {db_path}",
            "trace_id": trace_id,
        }

    try:
        with _read(db_path) as connection:
            resolved = _resolve_target(connection, target)
            if resolved is None:
                return {
                    "status": "no_match",
                    "target": empty_target,
                    "direction": direction,
                    "groups": [],
                    "stale": 0,
                    "truncated": False,
                    "trace_id": trace_id,
                }
            start_key, name, path = resolved
            cache: dict = {}
            max_items = max(limit * 4, limit + 50)
            entries: list[dict] = []
            stale_ids: set[int] = set()
            starts = [start_key]
            if not start_key.startswith("file:") and path:
                starts.append(f"file:{path}")
            directions = ("callers", "callees") if direction == "both" else (direction,)
            for walk_direction in directions:
                for walk_start in starts:
                    found, walk_stale = _collect(
                        connection,
                        walk_start,
                        walk_direction,
                        depth,
                        include_stale,
                        max_items,
                        cache,
                    )
                    entries.extend(found)
                    stale_ids |= walk_stale
            built_at = _built_at(connection)
            if direction == "both":
                for entry in entries:
                    entry["item"]["direction"] = entry["direction"]
            groups, truncated, total = _entries_to_groups(entries, limit)
    except sqlite3.Error as exc:
        return {
            "status": "error",
            "target": empty_target,
            "direction": direction,
            "error": str(exc),
            "trace_id": trace_id,
        }

    return {
        "status": "ok",
        "target": {"name": name, "file": path},
        "direction": direction,
        "groups": groups,
        "total": total,
        "stale": len(stale_ids),
        "truncated": truncated,
        "coverage": COVERAGE,
        "builtAt": built_at,
        "trace_id": trace_id,
    }
