"""SQLite graph store owned by the gateway.

The schema intentionally matches the host's former GraphDatabase so existing
databases migrate by copy. Writes stay in this module: no other component
builds SQL for the graph.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import urllib.parse
from dataclasses import dataclass
from typing import Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS files (
  path TEXT PRIMARY KEY, lang TEXT NOT NULL, size INTEGER NOT NULL,
  mtime REAL NOT NULL, content_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS symbols (
  sym_key TEXT PRIMARY KEY, path TEXT NOT NULL, name TEXT NOT NULL,
  kind TEXT NOT NULL, signature TEXT, start_line INTEGER NOT NULL,
  end_line INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_symbols_path ON symbols(path);
CREATE TABLE IF NOT EXISTS edges (
  id INTEGER PRIMARY KEY AUTOINCREMENT, from_key TEXT NOT NULL,
  to_key TEXT NOT NULL, kind TEXT NOT NULL, path TEXT NOT NULL,
  start_line INTEGER, end_line INTEGER, confidence REAL NOT NULL,
  resolver TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'ok'
);
CREATE INDEX IF NOT EXISTS idx_edges_to ON edges(to_key, kind);
CREATE INDEX IF NOT EXISTS idx_edges_from ON edges(from_key, kind);
CREATE INDEX IF NOT EXISTS idx_edges_path ON edges(path);
CREATE TABLE IF NOT EXISTS unresolved (
  path TEXT NOT NULL, raw_name TEXT NOT NULL, kind TEXT NOT NULL,
  line INTEGER, reason TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_unresolved_name ON unresolved(raw_name);
CREATE TABLE IF NOT EXISTS diagnostics (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def sym_key(
    path: str,
    name: str,
    kind: str,
    signature: str | None,
    start_line: int,
) -> str:
    raw = f"{path}|{name}|{kind}|{signature or ''}|{start_line}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def symbol_key(key: str) -> str:
    return "sym:" + key


def file_key(path: str) -> str:
    return "file:" + path


@dataclass(frozen=True)
class FileRow:
    path: str
    lang: str
    size: int
    mtime: float
    content_hash: str


@dataclass(frozen=True)
class SymbolRow:
    sym_key: str
    path: str
    name: str
    kind: str
    signature: str | None
    start_line: int
    end_line: int


@dataclass(frozen=True)
class EdgeRow:
    from_key: str
    to_key: str
    kind: str
    path: str
    start_line: int | None
    end_line: int | None
    confidence: float
    resolver: str
    status: str = "ok"


def unique_symbols(symbols: Iterable[SymbolRow]) -> list[SymbolRow]:
    """Drop symbols sharing a key, which minified files can emit repeatedly."""
    seen: set[str] = set()
    unique: list[SymbolRow] = []
    for symbol in symbols:
        if symbol.sym_key in seen:
            continue
        seen.add(symbol.sym_key)
        unique.append(symbol)
    return unique


@dataclass(frozen=True)
class UnresolvedRow:
    path: str
    raw_name: str
    kind: str
    line: int | None
    reason: str


class GraphStore:
    """One project's graph.sqlite."""

    def __init__(self, path: str, readonly: bool = False) -> None:
        self.path = os.path.abspath(path)
        if not readonly:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
        uri = "file:" + urllib.parse.quote(self.path) + ("?mode=ro" if readonly else "")
        self.conn = sqlite3.connect(uri, uri=True, timeout=5.0, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        if not readonly:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.execute("PRAGMA busy_timeout=5000")
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "GraphStore":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- reads ---------------------------------------------------------------

    def meta(self) -> dict[str, str]:
        return {
            row["key"]: row["value"]
            for row in self.conn.execute("SELECT key, value FROM meta")
        }

    def stats(self) -> dict[str, int]:
        def count(sql: str) -> int:
            return int(self.conn.execute(sql).fetchone()[0])

        files = count("SELECT COUNT(*) FROM files")
        symbols = count("SELECT COUNT(*) FROM symbols")
        edges = count("SELECT COUNT(*) FROM edges")
        stale = count("SELECT COUNT(*) FROM edges WHERE status='stale'")
        unresolved = count("SELECT COUNT(*) FROM unresolved")
        return {
            "nodes": files + symbols,
            "edges": edges,
            "stale": stale,
            "files": files,
            "symbols": symbols,
            "unresolved": unresolved,
        }

    def is_up_to_date(self, root: str, engine: str, schema: str) -> bool:
        meta = self.meta()
        return (
            meta.get("engine_version") == engine
            and meta.get("schema_version") == schema
            and meta.get("root") == root
        )

    def set_meta(self, key: str, value: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO meta(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def all_file_meta(self) -> dict[str, tuple[int, float, str]]:
        return {
            row["path"]: (row["size"], row["mtime"], row["content_hash"])
            for row in self.conn.execute(
                "SELECT path, size, mtime, content_hash FROM files"
            )
        }

    def file_hash(self, path: str) -> str | None:
        row = self.conn.execute(
            "SELECT content_hash FROM files WHERE path=?", (path,)
        ).fetchone()
        return row["content_hash"] if row else None

    def unresolved_paths(self, names: set[str]) -> set[str]:
        if not names:
            return set()
        found: set[str] = set()
        items = list(names)
        for start in range(0, len(items), 500):
            chunk = items[start : start + 500]
            marks = ",".join("?" for _ in chunk)
            rows = self.conn.execute(
                f"SELECT DISTINCT path FROM unresolved WHERE raw_name IN ({marks})",
                chunk,
            )
            found |= {row["path"] for row in rows}
        return found

    # -- writes --------------------------------------------------------------

    def update_file_meta(self, path: str, size: int, mtime: float) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE files SET size=?, mtime=? WHERE path=?", (size, mtime, path)
            )

    def delete_dangling_stale_edges(self) -> None:
        with self.conn:
            self.conn.execute(
                """
                DELETE FROM edges
                WHERE status='stale'
                  AND to_key NOT IN (SELECT 'sym:'||sym_key FROM symbols)
                  AND to_key NOT IN (SELECT 'file:'||path FROM files)
                """
            )

    def replace_graph(
        self,
        meta: dict[str, str],
        files: Iterable[FileRow],
        symbols: Iterable[SymbolRow],
        edges: Iterable[EdgeRow],
        unresolved: Iterable[UnresolvedRow],
        diagnostics: dict[str, str],
    ) -> None:
        symbols = unique_symbols(symbols)
        with self.conn:
            for table in (
                "edges",
                "symbols",
                "files",
                "unresolved",
                "diagnostics",
                "meta",
            ):
                self.conn.execute(f"DELETE FROM {table}")
            self.conn.executemany(
                "INSERT INTO meta(key, value) VALUES (?, ?)", meta.items()
            )
            self.conn.executemany(
                "INSERT INTO files(path, lang, size, mtime, content_hash) "
                "VALUES (?, ?, ?, ?, ?)",
                [(f.path, f.lang, f.size, f.mtime, f.content_hash) for f in files],
            )
            self.conn.executemany(
                "INSERT INTO symbols(sym_key, path, name, kind, signature, "
                "start_line, end_line) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        s.sym_key,
                        s.path,
                        s.name,
                        s.kind,
                        s.signature,
                        s.start_line,
                        s.end_line,
                    )
                    for s in symbols
                ],
            )
            self.conn.executemany(
                "INSERT INTO edges(from_key, to_key, kind, path, start_line, "
                "end_line, confidence, resolver, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        e.from_key,
                        e.to_key,
                        e.kind,
                        e.path,
                        e.start_line,
                        e.end_line,
                        e.confidence,
                        e.resolver,
                        e.status,
                    )
                    for e in edges
                ],
            )
            self.conn.executemany(
                "INSERT INTO unresolved(path, raw_name, kind, line, reason) "
                "VALUES (?, ?, ?, ?, ?)",
                [(u.path, u.raw_name, u.kind, u.line, u.reason) for u in unresolved],
            )
            self.conn.executemany(
                "INSERT INTO diagnostics(key, value) VALUES (?, ?)",
                diagnostics.items(),
            )

    def replace_file(
        self,
        file: FileRow,
        symbols: list[SymbolRow],
        edges: list[EdgeRow],
        unresolved: list[UnresolvedRow],
    ) -> None:
        symbols = unique_symbols(symbols)
        with self.conn:
            old = self.conn.execute(
                "SELECT sym_key, name, kind, signature FROM symbols WHERE path=?",
                (file.path,),
            ).fetchall()
            old_keys = [row["sym_key"] for row in old]
            for table in ("symbols", "edges", "unresolved", "files"):
                self.conn.execute(f"DELETE FROM {table} WHERE path=?", (file.path,))
            self.conn.execute(
                "INSERT INTO files(path, lang, size, mtime, content_hash) "
                "VALUES (?, ?, ?, ?, ?)",
                (file.path, file.lang, file.size, file.mtime, file.content_hash),
            )
            self.conn.executemany(
                "INSERT INTO symbols(sym_key, path, name, kind, signature, "
                "start_line, end_line) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        s.sym_key,
                        s.path,
                        s.name,
                        s.kind,
                        s.signature,
                        s.start_line,
                        s.end_line,
                    )
                    for s in symbols
                ],
            )
            self.conn.executemany(
                "INSERT INTO edges(from_key, to_key, kind, path, start_line, "
                "end_line, confidence, resolver, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        e.from_key,
                        e.to_key,
                        e.kind,
                        e.path,
                        e.start_line,
                        e.end_line,
                        e.confidence,
                        e.resolver,
                        e.status,
                    )
                    for e in edges
                ],
            )
            self.conn.executemany(
                "INSERT INTO unresolved(path, raw_name, kind, line, reason) "
                "VALUES (?, ?, ?, ?, ?)",
                [(u.path, u.raw_name, u.kind, u.line, u.reason) for u in unresolved],
            )
            self._rebind(old, symbols)
            self.conn.execute(
                "UPDATE edges SET status='ok' WHERE to_key=? AND status='stale'",
                (file_key(file.path),),
            )
            self._mark_incoming_stale(
                [key for key in old_keys if key not in {s.sym_key for s in symbols}]
            )

    def _rebind(self, old: list[sqlite3.Row], symbols: list[SymbolRow]) -> None:
        old_keys = {row["sym_key"] for row in old}
        new_keys = {s.sym_key for s in symbols}
        rebound: set[str] = set()
        for row in old:
            if row["sym_key"] in new_keys:
                continue
            match = next(
                (
                    s
                    for s in symbols
                    if s.sym_key not in old_keys
                    and s.sym_key not in rebound
                    and s.name == row["name"]
                    and s.kind == row["kind"]
                    and s.signature == row["signature"]
                ),
                None,
            )
            if match is None:
                continue
            rebound.add(match.sym_key)
            self.conn.execute(
                "UPDATE edges SET to_key=?, status='ok' WHERE to_key=?",
                (symbol_key(match.sym_key), symbol_key(row["sym_key"])),
            )

    def _mark_incoming_stale(self, keys: list[str]) -> None:
        if not keys:
            return
        targets = [symbol_key(key) for key in keys]
        for start in range(0, len(targets), 500):
            chunk = targets[start : start + 500]
            marks = ",".join("?" for _ in chunk)
            self.conn.execute(
                f"UPDATE edges SET status='stale' "
                f"WHERE to_key IN ({marks}) AND status='ok'",
                chunk,
            )

    def delete_path_subtree(self, path: str) -> None:
        prefix = path if path.endswith("/") else path + "/"
        with self.conn:
            rows = self.conn.execute(
                "SELECT path FROM files WHERE path=? OR path LIKE ? ESCAPE '\\'",
                (path, _like_escaped(prefix) + "%"),
            ).fetchall()
            paths = [row["path"] for row in rows]
            old_keys: list[str] = []
            for item in paths:
                old_keys.extend(
                    row["sym_key"]
                    for row in self.conn.execute(
                        "SELECT sym_key FROM symbols WHERE path=?", (item,)
                    )
                )
                for table in ("symbols", "edges", "unresolved", "files"):
                    self.conn.execute(f"DELETE FROM {table} WHERE path=?", (item,))
            for start in range(0, len(old_keys), 500):
                self._mark_incoming_stale(old_keys[start : start + 500])
            file_keys = [file_key(item) for item in paths]
            for start in range(0, len(file_keys), 500):
                chunk = file_keys[start : start + 500]
                marks = ",".join("?" for _ in chunk)
                self.conn.execute(
                    f"UPDATE edges SET status='stale' "
                    f"WHERE to_key IN ({marks}) AND status='ok'",
                    chunk,
                )


def _like_escaped(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
