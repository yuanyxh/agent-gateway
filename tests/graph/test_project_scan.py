"""Standalone graph build: scan, write, incremental update, removal."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from agent_gateway.core import config_loader
from agent_gateway.services.graph import indexer, project_scan


class ProjectScanTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._env = {
            "GATEWAY_HOME": str(self.root / "home"),
            "GATEWAY_PROJECT_DIR": ".agent-gateway",
        }
        self._saved = {key: os.environ.get(key) for key in self._env}
        os.environ.update(self._env)
        config_loader.reset_cache()
        (self.root / "a.py").write_text("def helper():\n    return 1\n")
        (self.root / "b.py").write_text(
            "from a import helper\n\ndef main():\n    return helper()\n"
        )

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        config_loader.reset_cache()
        self._tmp.cleanup()

    def test_sync_builds_agent_gateway_db(self):
        result = project_scan.sync(str(self.root), "2", "1")
        self.assertTrue(result["ok"])
        self.assertEqual(result["stats"]["files"], 2)
        self.assertEqual(result["stats"]["symbols"], 2)
        self.assertEqual(result["stats"]["edges"], 2)
        self.assertTrue((self.root / ".agent-gateway" / "graph.sqlite").is_file())
        self.assertFalse((self.root / ".utility").exists())

    def test_sync_is_idempotent(self):
        project_scan.sync(str(self.root), "2", "1")
        before = (self.root / ".agent-gateway" / "graph.sqlite").stat().st_mtime_ns
        project_scan.sync(str(self.root), "2", "1")
        after = (self.root / ".agent-gateway" / "graph.sqlite").stat().st_mtime_ns
        self.assertEqual(before, after)

    def test_index_file_writes_and_remove_deletes(self):
        project_scan.sync(str(self.root), "2", "1")
        response = indexer.handle_line(
            json.dumps({"op": "index_file", "root": str(self.root), "path": "b.py"})
        )
        self.assertTrue(response["ok"])
        removed = indexer.handle_line(
            json.dumps({"op": "remove_file", "root": str(self.root), "path": "b.py"})
        )
        self.assertTrue(removed["ok"])
        status = indexer.handle_line(
            json.dumps({"op": "status", "root": str(self.root)})
        )
        self.assertEqual(status["stats"]["files"], 1)
        self.assertEqual(status["stats"]["edges"], 0)

    def test_status_reports_missing_db(self):
        status = indexer.handle_line(
            json.dumps({"op": "status", "root": str(self.root)})
        )
        self.assertFalse(status["up_to_date"])
        self.assertEqual(status["stats"]["files"], 0)

    def test_partial_parse_is_recorded_in_diagnostics(self):
        (self.root / "broken.py").write_text("def broken(:\n    pass\n")
        result = project_scan.sync(str(self.root), "2", "1")
        self.assertEqual(result["partial_parse_files"], 1)
        conn = sqlite3.connect(self.root / ".agent-gateway" / "graph.sqlite")
        try:
            value = conn.execute(
                "SELECT value FROM diagnostics WHERE key='partial_parse_files'"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(json.loads(value), ["broken.py"])


if __name__ == "__main__":
    unittest.main()
