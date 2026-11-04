"""Project-local state directory resolution and legacy copy."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_gateway.core import config_loader, project_paths


class ProjectPathsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        config_loader.reset_cache()

    def tearDown(self):
        config_loader.reset_cache()
        self._tmp.cleanup()

    def test_defaults_to_agent_gateway(self):
        with mock.patch.dict(os.environ, {"GATEWAY_HOME": str(self.root)}):
            self.assertEqual(
                project_paths.project_state_dir(str(self.root)),
                self.root / ".agent-gateway",
            )

    def test_env_overrides_directory_name(self):
        env = {"GATEWAY_HOME": str(self.root), "GATEWAY_PROJECT_DIR": ".state"}
        with mock.patch.dict(os.environ, env):
            self.assertEqual(
                project_paths.project_state_dir("/tmp/project"),
                Path("/tmp/project/.state"),
            )

    def test_env_accepts_absolute_path(self):
        custom = str(self.root / "custom")
        env = {"GATEWAY_HOME": str(self.root), "GATEWAY_PROJECT_DIR": custom}
        with mock.patch.dict(os.environ, env):
            self.assertEqual(
                project_paths.project_state_dir("/tmp/project"), Path(custom)
            )

    def test_legacy_file_is_copied_not_moved(self):
        legacy = self.root / ".utility"
        legacy.mkdir()
        source = legacy / "graph.sqlite"
        source.write_bytes(b"old")
        target = project_paths.graph_db_path(str(self.root))
        project_paths.copy_legacy(source, target)
        self.assertEqual(target.read_bytes(), b"old")
        self.assertTrue(source.exists())

    def test_legacy_copy_keeps_wal_rows(self):
        legacy = self.root / ".utility"
        legacy.mkdir()
        source = legacy / "graph.sqlite"
        writer = sqlite3.connect(str(source))
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE symbols (name TEXT)")
        writer.execute("INSERT INTO symbols VALUES ('kept')")
        writer.commit()
        target = project_paths.graph_db_path(str(self.root))
        project_paths.copy_legacy(source, target)
        writer.close()
        with sqlite3.connect(str(target)) as conn:
            self.assertEqual(
                conn.execute("SELECT name FROM symbols").fetchone()[0], "kept"
            )

    def test_readable_graph_prefers_new_then_legacy(self):
        legacy = self.root / ".utility"
        legacy.mkdir()
        (legacy / "graph.sqlite").write_bytes(b"old")
        self.assertEqual(
            project_paths.readable_graph_db(str(self.root)),
            legacy / "graph.sqlite",
        )
        new = self.root / ".agent-gateway"
        new.mkdir()
        (new / "graph.sqlite").write_bytes(b"new")
        self.assertEqual(
            project_paths.readable_graph_db(str(self.root)),
            new / "graph.sqlite",
        )


if __name__ == "__main__":
    unittest.main()
