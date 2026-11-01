"""project_lookup / project_impact over a temporary graph.sqlite."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest

from agent_gateway.services.mcp import graph_tools

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE files (
  path TEXT PRIMARY KEY, lang TEXT NOT NULL, size INTEGER NOT NULL,
  mtime REAL NOT NULL, content_hash TEXT NOT NULL
);
CREATE TABLE symbols (
  sym_key TEXT PRIMARY KEY, path TEXT NOT NULL, name TEXT NOT NULL,
  kind TEXT NOT NULL, signature TEXT, start_line INTEGER NOT NULL,
  end_line INTEGER NOT NULL
);
CREATE INDEX idx_symbols_name ON symbols(name);
CREATE TABLE edges (
  id INTEGER PRIMARY KEY AUTOINCREMENT, from_key TEXT NOT NULL,
  to_key TEXT NOT NULL, kind TEXT NOT NULL, path TEXT NOT NULL,
  start_line INTEGER, end_line INTEGER, confidence REAL NOT NULL,
  resolver TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'ok'
);
CREATE TABLE unresolved (
  path TEXT NOT NULL, raw_name TEXT NOT NULL, kind TEXT NOT NULL,
  line INTEGER, reason TEXT NOT NULL
);
"""


def _create_db(path: str) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(SCHEMA)
        connection.execute(
            "INSERT INTO meta VALUES ('built_at', '2026-11-01T00:00:00Z')"
        )
        connection.executemany(
            "INSERT INTO files VALUES (?, ?, ?, ?, ?)",
            [
                ("Sources/Scanner/ProjectScanner.swift", "swift", 10, 0, "h1"),
                ("Sources/App/AppDelegate.swift", "swift", 10, 0, "h2"),
            ],
        )
        connection.executemany(
            "INSERT INTO symbols VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    "s1",
                    "Sources/Scanner/ProjectScanner.swift",
                    "ProjectScanner",
                    "class",
                    None,
                    5,
                    120,
                ),
                (
                    "s2",
                    "Sources/Scanner/ProjectScanner.swift",
                    "scan",
                    "method",
                    None,
                    20,
                    30,
                ),
                (
                    "s3",
                    "Sources/App/AppDelegate.swift",
                    "applicationDidFinishLaunching",
                    "function",
                    None,
                    40,
                    50,
                ),
                ("s4", "Sources/Old.swift", "oldCaller", "function", None, 10, 12),
            ],
        )
        connection.executemany(
            "INSERT INTO edges (from_key,to_key,kind,path,start_line,end_line,"
            "confidence,resolver,status) VALUES (?,?,?,?,?,?,?,?,?)",
            [
                (
                    "sym:s3",
                    "sym:s1",
                    "call",
                    "Sources/App/AppDelegate.swift",
                    42,
                    42,
                    0.9,
                    "import_scoped",
                    "ok",
                ),
                (
                    "sym:s4",
                    "sym:s1",
                    "call",
                    "Sources/Old.swift",
                    10,
                    10,
                    0.7,
                    "global_unique",
                    "stale",
                ),
                (
                    "sym:s1",
                    "sym:s2",
                    "call",
                    "Sources/Scanner/ProjectScanner.swift",
                    20,
                    20,
                    1.0,
                    "local",
                    "ok",
                ),
            ],
        )
        connection.commit()
    finally:
        connection.close()


class LookupTest(unittest.TestCase):
    def test_returns_definition_and_caller_context(self):
        with tempfile.TemporaryDirectory() as root:
            db = os.path.join(root, "graph.sqlite")
            _create_db(db)
            result = graph_tools.tool_project_lookup(db, "ProjectScanner")
        self.assertEqual(result["status"], "ok")
        hit = result["results"][0]
        self.assertEqual(hit["name"], "ProjectScanner")
        self.assertEqual(hit["kind"], "class")
        self.assertEqual(hit["file"], "Sources/Scanner/ProjectScanner.swift")
        self.assertEqual(hit["startLine"], 5)
        self.assertEqual(hit["endLine"], 120)
        self.assertEqual(hit["callerCount"], 1)
        self.assertEqual(hit["topCallers"][0]["file"], "Sources/App/AppDelegate.swift")
        self.assertEqual(hit["resolver"], "treesitter")
        self.assertTrue(result["trace_id"].startswith("gl_"))

    def test_falls_back_to_file_lookup(self):
        with tempfile.TemporaryDirectory() as root:
            db = os.path.join(root, "graph.sqlite")
            _create_db(db)
            result = graph_tools.tool_project_lookup(db, "ProjectScanner.swift")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["results"][0]["kind"], "file")

    def test_empty_query_returns_overview(self):
        with tempfile.TemporaryDirectory() as root:
            db = os.path.join(root, "graph.sqlite")
            _create_db(db)
            result = graph_tools.tool_project_lookup(db, "")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["overview"]["files"], 2)
        self.assertEqual(result["overview"]["builtAt"], "2026-11-01T00:00:00Z")
        self.assertIn("call", result["overview"]["edgeKinds"])

    def test_missing_database_is_an_error(self):
        result = graph_tools.tool_project_lookup("/nonexistent/graph.sqlite", "x")
        self.assertEqual(result["status"], "error")


class ImpactTest(unittest.TestCase):
    def _db(self, root: str) -> str:
        db = os.path.join(root, "graph.sqlite")
        _create_db(db)
        return db

    def test_callers_group_by_resolver_and_exclude_stale(self):
        with tempfile.TemporaryDirectory() as root:
            db = self._db(root)
            result = graph_tools.tool_project_impact(db, "ProjectScanner")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(
            result["target"],
            {"name": "ProjectScanner", "file": "Sources/Scanner/ProjectScanner.swift"},
        )
        self.assertEqual(result["stale"], 1)
        self.assertFalse(result["truncated"])
        self.assertEqual(len(result["groups"]), 1)
        group = result["groups"][0]
        self.assertEqual(group["resolver"], "import_scoped")
        self.assertFalse(group["weak"])
        self.assertEqual(group["confidence"], 0.9)
        item = group["items"][0]
        self.assertEqual(item["file"], "Sources/App/AppDelegate.swift")
        self.assertEqual(item["symbol"], "applicationDidFinishLaunching")
        self.assertEqual(item["line"], 42)
        self.assertEqual(item["kind"], "call")
        self.assertEqual(item["status"], "ok")
        self.assertEqual(item["hop"], 1)
        self.assertTrue(result["trace_id"].startswith("gi_"))

    def test_include_stale_keeps_stale_items(self):
        with tempfile.TemporaryDirectory() as root:
            db = self._db(root)
            result = graph_tools.tool_project_impact(
                db, "ProjectScanner", include_stale=True
            )
        statuses = [
            item["status"] for group in result["groups"] for item in group["items"]
        ]
        self.assertIn("stale", statuses)

    def test_callees_and_depth(self):
        with tempfile.TemporaryDirectory() as root:
            db = self._db(root)
            result = graph_tools.tool_project_impact(
                db, "ProjectScanner", direction="callees", depth=1
            )
        names = [
            item["symbol"] for group in result["groups"] for item in group["items"]
        ]
        self.assertIn("scan", names)

    def test_reports_coverage_and_build_time(self):
        with tempfile.TemporaryDirectory() as root:
            db = self._db(root)
            result = graph_tools.tool_project_impact(db, "ProjectScanner")
        self.assertIn("call", result["coverage"]["edges"])
        self.assertIn("multi_hop_re_export", result["coverage"]["not_covered"])
        self.assertEqual(result["builtAt"], "2026-11-01T00:00:00Z")

    def test_file_import_edge_is_traversed(self):
        with tempfile.TemporaryDirectory() as root:
            db = self._db(root)
            with sqlite3.connect(db) as connection:
                connection.execute(
                    "INSERT INTO edges (from_key,to_key,kind,path,start_line,"
                    "end_line,confidence,resolver,status) VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        "file:Sources/App/AppDelegate.swift",
                        "file:Sources/Scanner/ProjectScanner.swift",
                        "import",
                        "Sources/App/AppDelegate.swift",
                        1,
                        1,
                        1.0,
                        "import_path",
                        "ok",
                    ),
                )
            result = graph_tools.tool_project_impact(db, "ProjectScanner", depth=1)
        imports = [
            item
            for group in result["groups"]
            for item in group["items"]
            if item["kind"] == "import"
        ]
        self.assertEqual(imports[0]["file"], "Sources/App/AppDelegate.swift")

    def test_unknown_target_is_no_match(self):
        with tempfile.TemporaryDirectory() as root:
            db = self._db(root)
            result = graph_tools.tool_project_impact(db, "NoSuchSymbol")
        self.assertEqual(result["status"], "no_match")
        self.assertEqual(result["groups"], [])


if __name__ == "__main__":
    unittest.main()
