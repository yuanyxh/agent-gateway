"""MCP project search/structure/exports tools."""

from __future__ import annotations

import os
import sqlite3


from agent_gateway.core.db import conn as dbconn

from agent_gateway.services.mcp.search_constants import (
    DEFAULT_PROJECT_SEARCH_LIMIT,
    DEFAULT_PROJECT_STRUCTURE_LIMIT,
)
from agent_gateway.services.mcp.search_symbols import (
    _search_files,
    _search_packages,
    _search_symbols_fts,
    _search_symbols_like,
    expand_query,
)
from agent_gateway.services.mcp.search_vectors import _search_vectors


def tool_project_search(
    db_path: str,
    query: str,
    file_path: str = "",
    kind: str = "",
    limit: int = DEFAULT_PROJECT_SEARCH_LIMIT,
) -> dict:
    """Multi-layer project search.

    Chains: FTS5 symbols → LIKE symbols → files → packages → vector similarity.
    Always runs all layers, deduplicates results, returns combined output.
    """
    if not os.path.isfile(db_path):
        return {"error": f"Database not found: {db_path}"}

    expanded = expand_query(query)
    active_layers: list[str] = []
    all_found: list[dict] = []
    seen: set[tuple[str, str]] = set()  # (name, file_path) dedup key

    def _add(results: list[dict], layer: str):
        if not results:
            return
        active_layers.append(layer)
        for r in results:
            key = (r["name"], r.get("file") or "")
            if key not in seen:
                seen.add(key)
                all_found.append(r)

    # Layer 1a: FTS5 symbol search (primary)
    fts_results = _search_symbols_fts(db_path, expanded, file_path, kind)
    if fts_results is not None:
        _add(fts_results, "fts5")
    else:
        # Layer 1b: LIKE fallback when FTS5 unavailable
        like_results = _search_symbols_like(db_path, query, file_path, kind)
        _add(like_results, "like")

    # Layer 2: File name / path search
    _add(_search_files(db_path, query), "file")

    # Layer 3: Package name / description search
    _add(_search_packages(db_path, query), "package")

    # Layer 4: Vector similarity search (only if few results from above)
    if len(all_found) < 5:
        _add(_search_vectors(db_path, query), "vector")

    total = len(all_found)

    # ── Status determination ──
    # Prioritize symbol-level results for backward-compatible status
    symbol_found = [r for r in all_found if r["kind"] not in ("file", "package")]

    if total == 0:
        return {
            "status": "no_match",
            "query": query,
            "expandedQuery": expanded,
            "layers": active_layers,
            "results": [],
            "total": 0,
        }

    # Determine best single result for "exact" status
    best = None
    if len(symbol_found) == 1:
        best = symbol_found[0]
    elif len(symbol_found) > 1:
        s0, s1 = symbol_found[0], symbol_found[1]
        if s0["name"] != s1["name"] or s0["file"] == s1["file"]:
            best = symbol_found[0]

    if best is not None and total == 1:
        return {
            "status": "exact",
            "query": query,
            "expandedQuery": expanded,
            "layers": active_layers,
            "result": best,
            "results": all_found[:limit],
            "total": total,
        }

    if best is not None:
        return {
            "status": "exact",
            "query": query,
            "expandedQuery": expanded,
            "layers": active_layers,
            "result": best,
            "results": all_found[:limit],
            "total": total,
        }

    # Check for ambiguous symbols (same name, different files)
    if len(symbol_found) > 1 and symbol_found[0]["name"] == symbol_found[1]["name"]:
        options = []
        for i, r in enumerate(symbol_found[:5], 1):
            loc = r["file"]
            sig = f" {r['signature']}" if r.get("signature") else ""
            options.append(f"  {i}. {r['name']} ({r['kind']}) — {loc}{sig}")
        return {
            "status": "ambiguous",
            "query": query,
            "expandedQuery": expanded,
            "layers": active_layers,
            "disambiguation": (
                f"Found {len(symbol_found)} matches with the same name in different files:\n"
                + "\n".join(options)
                + "\nPass file_path or kind to narrow it down."
            ),
            "results": all_found[:limit],
            "total": total,
        }

    # Fallback: non-symbol results only (files, packages, vector)
    return {
        "status": "no_match",
        "query": query,
        "expandedQuery": expanded,
        "layers": active_layers,
        "results": all_found[:limit],
        "total": total,
    }


def tool_project_structure(
    db_path: str, subpath: str = "", limit: int = DEFAULT_PROJECT_STRUCTURE_LIMIT
) -> dict:
    if not os.path.isfile(db_path):
        return {"error": f"Database not found: {db_path}"}
    with dbconn(db_path) as conn:
        conn.row_factory = sqlite3.Row

        MAX_ITEMS = limit

        if subpath:
            prefix = subpath.rstrip("/") + "/"
            dirs = conn.execute(
                "SELECT path, module_name, file_count, has_package_manifest "
                "FROM directories WHERE path LIKE ? AND path NOT LIKE ? "
                "ORDER BY path LIMIT ?",
                (prefix + "%", prefix + "%/%", MAX_ITEMS + 1),
            ).fetchall()
            files = conn.execute(
                "SELECT path, ext FROM files WHERE dir_path = ? ORDER BY path LIMIT ?",
                (subpath, MAX_ITEMS + 1),
            ).fetchall()
        else:
            dirs = conn.execute(
                "SELECT path, module_name, file_count, has_package_manifest "
                "FROM directories WHERE path NOT LIKE '%/%' "
                "ORDER BY path LIMIT ?",
                (MAX_ITEMS + 1,),
            ).fetchall()
            files = conn.execute(
                "SELECT path, ext FROM files WHERE dir_path = '' ORDER BY path LIMIT ?",
                (MAX_ITEMS + 1,),
            ).fetchall()

    result = {
        "directories": [
            {
                "path": r["path"],
                "name": r["module_name"],
                "fileCount": r["file_count"],
                "isManifest": bool(r["has_package_manifest"]),
            }
            for r in dirs[:MAX_ITEMS]
        ],
        "files": [{"path": f["path"], "ext": f["ext"]} for f in files[:MAX_ITEMS]],
        "totalDirs": len(dirs),
        "totalFiles": len(files),
    }

    if len(dirs) > MAX_ITEMS or len(files) > MAX_ITEMS:
        result["truncated"] = True
        result["hint"] = "Result truncated; pass a subpath to narrow it down"

    return result


def tool_project_exports(db_path: str, path: str) -> dict:
    if not os.path.isfile(db_path):
        return {"error": f"Database not found: {db_path}"}
    with dbconn(db_path) as conn:
        conn.row_factory = sqlite3.Row
        is_dir = conn.execute(
            "SELECT 1 FROM directories WHERE path = ?", (path,)
        ).fetchone()
        if is_dir:
            files = conn.execute(
                "SELECT path, name, ext, sym_count FROM files WHERE dir_path = ? ORDER BY path",
                (path,),
            ).fetchall()
            result_files = []
            for f in files:
                syms = conn.execute(
                    "SELECT name, kind, signature, start_line, end_line FROM symbols "
                    "WHERE file_path = ? AND (visibility = 'public' OR visibility IS NULL) "
                    "ORDER BY kind, name",
                    (f["path"],),
                ).fetchall()
                result_files.append(
                    {
                        "path": f["path"],
                        "symbols": [
                            {
                                "name": s["name"],
                                "kind": s["kind"],
                                "signature": s["signature"],
                                "startLine": s["start_line"],
                                "endLine": s["end_line"],
                            }
                            for s in syms
                        ],
                    }
                )
            return {"path": path, "isDirectory": True, "files": result_files}
        else:
            syms = conn.execute(
                "SELECT name, kind, signature, visibility, start_line, end_line FROM symbols "
                "WHERE file_path = ? ORDER BY kind, name",
                (path,),
            ).fetchall()
            return {
                "path": path,
                "isDirectory": False,
                "symbols": [
                    {
                        "name": s["name"],
                        "kind": s["kind"],
                        "signature": s["signature"],
                        "startLine": s["start_line"],
                        "endLine": s["end_line"],
                        "visibility": s["visibility"],
                    }
                    for s in syms
                ],
                "total": len(syms),
            }
