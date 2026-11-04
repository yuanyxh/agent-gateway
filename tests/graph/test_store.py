"""Graph store write invariants: key rebinding and stale edges."""

from __future__ import annotations

import os
import tempfile
import unittest

from agent_gateway.services.graph import store


class StoreWriteTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = store.GraphStore(os.path.join(self._tmp.name, "graph.sqlite"))

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def _file(self, path: str, digest: str = "h") -> store.FileRow:
        return store.FileRow(path, "python", 10, 0.0, digest)

    def _symbol(self, path: str, name: str, line: int) -> store.SymbolRow:
        key = store.sym_key(path, name, "function", None, line)
        return store.SymbolRow(key, path, name, "function", None, line, line + 2)

    def test_replace_file_rebinds_shifted_symbol(self):
        old = self._symbol("a.py", "helper", 1)
        self.db.replace_graph(
            {"root": "/p", "engine_version": "2", "schema_version": "1"},
            [self._file("a.py"), self._file("b.py")],
            [old],
            [
                store.EdgeRow(
                    store.file_key("b.py"),
                    store.symbol_key(old.sym_key),
                    "call",
                    "b.py",
                    1,
                    1,
                    1.0,
                    "import_path",
                )
            ],
            [],
            {},
        )
        shifted = self._symbol("a.py", "helper", 5)
        self.db.replace_file(self._file("a.py", "h2"), [shifted], [], [])
        row = self.db.conn.execute("SELECT to_key, status FROM edges").fetchone()
        self.assertEqual(row["to_key"], store.symbol_key(shifted.sym_key))
        self.assertEqual(row["status"], "ok")

    def test_duplicate_symbol_keys_are_collapsed(self):
        duplicate = self._symbol("marked.js", "e", 44)
        self.db.replace_graph(
            {"root": "/p", "engine_version": "2", "schema_version": "1"},
            [self._file("marked.js")],
            [duplicate, duplicate],
            [],
            [],
            {},
        )
        count = self.db.conn.execute("SELECT count(*) FROM symbols").fetchone()[0]
        self.assertEqual(count, 1)
        self.db.replace_file(self._file("marked.js"), [duplicate, duplicate], [], [])
        count = self.db.conn.execute("SELECT count(*) FROM symbols").fetchone()[0]
        self.assertEqual(count, 1)

    def test_delete_marks_incoming_stale_then_reclaims(self):
        target = self._symbol("a.py", "helper", 1)
        self.db.replace_graph(
            {"root": "/p", "engine_version": "2", "schema_version": "1"},
            [self._file("a.py"), self._file("b.py")],
            [target],
            [
                store.EdgeRow(
                    store.file_key("b.py"),
                    store.symbol_key(target.sym_key),
                    "call",
                    "b.py",
                    1,
                    1,
                    1.0,
                    "import_path",
                )
            ],
            [],
            {},
        )
        self.db.delete_path_subtree("a.py")
        row = self.db.conn.execute("SELECT status FROM edges").fetchone()
        self.assertEqual(row["status"], "stale")
        self.db.delete_dangling_stale_edges()
        self.assertEqual(
            self.db.conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0], 0
        )

    def test_stats_counts(self):
        self.db.replace_graph(
            {"root": "/p"},
            [self._file("a.py")],
            [self._symbol("a.py", "f", 1)],
            [],
            [store.UnresolvedRow("a.py", "missing", "call", 1, "no_binding")],
            {},
        )
        self.assertEqual(
            self.db.stats(),
            {
                "nodes": 2,
                "edges": 0,
                "stale": 0,
                "files": 1,
                "symbols": 1,
                "unresolved": 1,
            },
        )


if __name__ == "__main__":
    unittest.main()
