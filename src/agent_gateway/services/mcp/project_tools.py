"""MCP project search/structure tools."""

from __future__ import annotations

import os
import re
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
from agent_gateway.services.retrieval.observe import traced

_WORD_SPLIT = re.compile(r"[A-Z]+(?=[A-Z][a-z]|\d|$)|[A-Z]?[a-z]+|\d+")
_CJK = re.compile(r"[一-鿿]")


def _identifier_words(*values: str) -> set[str]:
    words: set[str] = set()
    for value in values:
        if not value:
            continue
        for chunk in re.findall(r"[A-Za-z][A-Za-z0-9]*", str(value)):
            for word in _WORD_SPLIT.findall(chunk):
                lowered = word.lower()
                if len(lowered) >= 3 and not lowered.isdigit():
                    words.add(lowered)
    return words


def _query_terms(query: str) -> list[str]:
    stripped = (query or "").strip()
    if not stripped or _CJK.search(stripped):
        return []
    if not any(ch.isspace() for ch in stripped):
        token = stripped.lower()
        return [token] if len(token) >= 3 else []
    return [
        term
        for term in re.split(r"[^a-z0-9_]+", stripped.lower())
        if len(term) >= 3 and not term.isdigit()
    ]


def _has_lexical_overlap(result: dict, terms: list[str]) -> bool:
    words = _identifier_words(
        result.get("name"), result.get("signature"), result.get("file")
    )
    return any(term in words for term in terms)


def _attach_search_context(db_path: str, results: list[dict]) -> None:
    """Add module and importer context so callers do not need a follow-up grep."""
    paths = sorted(
        {
            str(r.get("file"))
            for r in results
            if isinstance(r.get("file"), str) and r.get("file")
        }
    )
    if not paths:
        return
    for result in results:
        file_path = result.get("file")
        if isinstance(file_path, str) and file_path:
            result["module"] = os.path.dirname(file_path).replace(os.sep, "/") or "."
    try:
        with dbconn(db_path) as conn:
            placeholders = ",".join("?" for _ in paths)
            rows = conn.execute(
                "SELECT to_path, from_path FROM imports "
                f"WHERE is_external = 0 AND to_path IN ({placeholders})",
                paths,
            ).fetchall()
    except sqlite3.Error:
        return
    importers: dict[str, list[str]] = {}
    for to_path, from_path in rows:
        if to_path and from_path:
            importers.setdefault(str(to_path), []).append(str(from_path))
    for result in results:
        file_path = result.get("file")
        if not isinstance(file_path, str) or not file_path:
            continue
        owners = sorted(set(importers.get(file_path, [])))
        if owners:
            result["importedBy"] = owners[:3]
            result["importCount"] = len(owners)


def _project_search_impl(
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
                r["layer"] = layer
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
    if total:
        _attach_search_context(db_path, all_found)

    # Vector-only matches are weak evidence: do not promote them to exact, and
    # reject candidates without a lexical anchor because the embedding model
    # scores unrelated gibberish highly. Chinese queries carry no lexical terms
    # (symbols are English identifiers), so the vector layer's own similarity
    # floor is their only guard — they surface as weak matches, not rejects.
    if total and all(r.get("layer") == "vector" for r in all_found):
        terms = _query_terms(query)
        filtered = [r for r in all_found if not terms or _has_lexical_overlap(r, terms)]
        if not filtered:
            return {
                "status": "no_match",
                "query": query,
                "expandedQuery": expanded,
                "layers": active_layers,
                "results": [],
                "total": 0,
                "rejected": total,
            }
        payload = {
            "status": "weak_match",
            "query": query,
            "expandedQuery": expanded,
            "layers": active_layers,
            "results": filtered[:limit],
            "total": len(filtered),
            "hint": "vector-only matches; verify before relying on them",
        }
        if len(filtered) < total:
            payload["rejected"] = total - len(filtered)
        return payload

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


@traced("project_search", query_arg=1)
def tool_project_search(
    db_path: str,
    query: str,
    file_path: str = "",
    kind: str = "",
    limit: int = DEFAULT_PROJECT_SEARCH_LIMIT,
) -> dict:
    """Traced entry point; the retrieval arm calls the impl directly."""
    return _project_search_impl(db_path, query, file_path, kind, limit)


def tool_project_structure(
    db_path: str, subpath: str = "", limit: int = DEFAULT_PROJECT_STRUCTURE_LIMIT
) -> dict:
    if not os.path.isfile(db_path):
        return {"error": f"Database not found: {db_path}"}
    subpath = (subpath or "").strip().strip("/")
    with dbconn(db_path) as conn:
        conn.row_factory = sqlite3.Row

        MAX_ITEMS = max(1, int(limit))
        scanned = conn.execute("SELECT 1 FROM directories LIMIT 1").fetchone()
        if scanned is None:
            scanned = conn.execute("SELECT 1 FROM files LIMIT 1").fetchone()

        path_exists = None
        if subpath:
            path_exists = conn.execute(
                "SELECT 1 FROM directories WHERE path = ? LIMIT 1", (subpath,)
            ).fetchone()
            prefix = subpath + "/"
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
        "status": "ok",
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

    if scanned is None:
        result["status"] = "not_scanned"
        result["hint"] = "The dependency database has no scanned files or directories"
    elif not dirs and not files:
        if subpath and path_exists is None:
            result["status"] = "path_not_found"
            result["hint"] = f"No scanned directory or file matches: {subpath}"
        else:
            result["status"] = "empty"
            result["hint"] = "The selected directory has no scanned children or files"

    if len(dirs) > MAX_ITEMS or len(files) > MAX_ITEMS:
        result["truncated"] = True
        result["hint"] = "Result truncated; pass a subpath to narrow it down"

    return result
