import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_gateway.services.mcp.server_search import tool_project_dependencies


class ProjectDependenciesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "deps.sqlite")
        conn = sqlite3.connect(self.db)
        conn.executescript(
            """
            CREATE TABLE directories (path TEXT PRIMARY KEY);
            CREATE TABLE files (path TEXT PRIMARY KEY);
            CREATE TABLE imports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                from_path TEXT NOT NULL,
                raw_target TEXT NOT NULL,
                to_path TEXT,
                is_external BOOLEAN NOT NULL DEFAULT 0,
                kind TEXT NOT NULL,
                line INTEGER,
                confidence REAL NOT NULL DEFAULT 1.0,
                resolver TEXT
            );
            """
        )
        conn.execute("INSERT INTO directories(path) VALUES ('web/src')")
        conn.executemany(
            "INSERT INTO files(path) VALUES (?)",
            [("app/main.py",), ("app/util.py",), ("web/src/index.ts",)],
        )
        rows = [
            ("app/main.py", "os", None, 1, "static", 1, 1.0, "python-module"),
            (
                "app/main.py",
                ".util",
                "app/util.py",
                0,
                "static",
                2,
                1.0,
                "python-module",
            ),
            ("web/src/index.ts", "./a", None, 0, "static", 1, 1.0, "js-path"),
            (
                "app/other.py",
                "app.util",
                "app/util.py",
                0,
                "static",
                1,
                1.0,
                "python-module",
            ),
        ]
        conn.executemany(
            "INSERT INTO imports(from_path, raw_target, to_path, is_external, kind, line, confidence, resolver) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_outgoing_groups(self):
        result = tool_project_dependencies(self.db, "app/main.py", "outgoing")
        self.assertFalse(result["isDirectory"])
        self.assertEqual(
            [item["rawTarget"] for item in result["outgoing"]["resolved"]], [".util"]
        )
        self.assertEqual(
            [item["rawTarget"] for item in result["outgoing"]["external"]], ["os"]
        )
        self.assertEqual(result["outgoing"]["unresolved"], [])
        self.assertEqual(result["incoming"]["resolved"], [])
        self.assertEqual(result["counts"]["outgoingResolved"], 1)
        self.assertEqual(result["counts"]["outgoingExternal"], 1)

    def test_incoming_resolved(self):
        result = tool_project_dependencies(self.db, "app/util.py", "incoming")
        self.assertEqual(
            [item["fromPath"] for item in result["incoming"]["resolved"]],
            ["app/main.py", "app/other.py"],
        )

    def test_unresolved_is_separate_from_external(self):
        result = tool_project_dependencies(self.db, "web/src/index.ts", "both")
        self.assertEqual(
            [item["rawTarget"] for item in result["outgoing"]["unresolved"]], ["./a"]
        )
        self.assertEqual(result["outgoing"]["external"], [])

    def test_directory_glob(self):
        result = tool_project_dependencies(self.db, "web/src", "outgoing")
        self.assertTrue(result["isDirectory"])
        self.assertEqual(
            [item["fromPath"] for item in result["outgoing"]["unresolved"]],
            ["web/src/index.ts"],
        )

    def test_limit_and_truncated(self):
        result = tool_project_dependencies(self.db, "app/util.py", "incoming", limit=1)
        self.assertEqual(len(result["incoming"]["resolved"]), 1)
        self.assertEqual(result["counts"]["incomingResolved"], 2)
        self.assertTrue(result["truncated"])

    def test_missing_imports_table_degrades(self):
        path = str(Path(self.tmp.name) / "empty.sqlite")
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE files (path TEXT PRIMARY KEY)")
        conn.commit()
        conn.close()
        result = tool_project_dependencies(path, "app/main.py")
        self.assertTrue(result["degraded"])
        self.assertEqual(result["outgoing"]["resolved"], [])
        self.assertFalse(result["truncated"])


if __name__ == "__main__":
    unittest.main()
