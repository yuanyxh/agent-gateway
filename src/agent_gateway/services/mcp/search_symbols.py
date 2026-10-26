"""Symbol, file, and package search (FTS/LIKE)."""

from __future__ import annotations

import sqlite3


from agent_gateway.core.db import conn as dbconn
from agent_gateway.core.synonyms import load_synonyms


def expand_query(query: str) -> str:
    # Built-in defaults + user additions (config.yaml `mcp.synonyms`); merging and caching both live in the synonyms layer
    synonyms = load_synonyms()
    terms = query.lower().split()
    expanded = set(terms)
    for term in terms:
        if term in synonyms:
            for syn in synonyms[term].split():
                expanded.add(syn)
    return " OR ".join(f'"{t}"' if " " in t else t for t in expanded)


def _search_symbols_fts(
    db_path: str, expanded: str, file_path: str, kind: str
) -> list[dict] | None:
    """Layer 1a: FTS5 symbol search. Returns None if FTS5 unavailable (degrade to LIKE)."""
    try:
        with dbconn(db_path) as conn:
            conn.row_factory = sqlite3.Row
            sql = (
                "SELECT s.name, s.kind, s.signature, s.file_path, s.visibility, "
                "s.start_line, s.end_line "
                "FROM symbols s JOIN symbols_fts fts ON s.id = fts.rowid "
                "WHERE symbols_fts MATCH ?"
            )
            params: list[str] = [expanded]
            if file_path:
                sql += " AND s.file_path LIKE ?"
                params.append(f"%{file_path}%")
            if kind:
                sql += " AND s.kind = ?"
                params.append(kind)
            sql += " ORDER BY rank LIMIT 30"
            return [
                {
                    "name": r["name"],
                    "kind": r["kind"],
                    "signature": r["signature"],
                    "file": r["file_path"],
                    "visibility": r["visibility"],
                    "startLine": r["start_line"],
                    "endLine": r["end_line"],
                }
                for r in conn.execute(sql, params).fetchall()
            ]
    except sqlite3.OperationalError:
        return None


def _search_symbols_like(
    db_path: str, query: str, file_path: str, kind: str
) -> list[dict]:
    """Layer 1b: LIKE fallback when FTS5 is unavailable."""
    with dbconn(db_path) as conn:
        conn.row_factory = sqlite3.Row
        sql = (
            "SELECT name, kind, signature, file_path, visibility, start_line, end_line "
            "FROM symbols WHERE (name LIKE ? OR signature LIKE ?)"
        )
        params: list[str] = [f"%{query}%", f"%{query}%"]
        if file_path:
            sql += " AND file_path LIKE ?"
            params.append(f"%{file_path}%")
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        sql += " LIMIT 30"
        return [
            {
                "name": r["name"],
                "kind": r["kind"],
                "signature": r["signature"],
                "file": r["file_path"],
                "visibility": r["visibility"],
                "startLine": r["start_line"],
                "endLine": r["end_line"],
            }
            for r in conn.execute(sql, params).fetchall()
        ]


def _search_files(db_path: str, query: str) -> list[dict]:
    """Layer 2: Search files table by name or path."""
    with dbconn(db_path) as conn:
        conn.row_factory = sqlite3.Row
        like = f"%{query}%"
        rows = conn.execute(
            "SELECT path, name, ext FROM files "
            "WHERE name LIKE ? OR path LIKE ? "
            "ORDER BY CASE WHEN name LIKE ? THEN 0 ELSE 1 END "
            "LIMIT 10",
            [like, like, f"{query}%"],
        ).fetchall()
        return [
            {
                "name": r["name"],
                "kind": "file",
                "file": r["path"],
                "signature": f".{r['ext']}",
                "visibility": None,
            }
            for r in rows
        ]


def _search_packages(db_path: str, query: str) -> list[dict]:
    """Layer 3: Search packages table by name or description."""
    with dbconn(db_path) as conn:
        conn.row_factory = sqlite3.Row
        like = f"%{query}%"
        rows = conn.execute(
            "SELECT name, version, type, description FROM packages "
            "WHERE name LIKE ? OR description LIKE ? "
            "LIMIT 10",
            [like, like],
        ).fetchall()
        return [
            {
                "name": r["name"],
                "kind": "package",
                "file": None,
                "signature": f"{r['type']}{'@' + r['version'] if r['version'] else ''}",
                "visibility": None,
            }
            for r in rows
        ]
