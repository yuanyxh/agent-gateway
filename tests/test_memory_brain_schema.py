"""Tests for schema v3 migration, rollback and old-export import."""

import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QDRANT_PATH", tempfile.mkdtemp(prefix="agent-gateway-test-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_gateway.services.memory import store  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

V1_SCHEMA = """
CREATE TABLE neurons (
  neuron_id TEXT PRIMARY KEY, episode_id TEXT UNIQUE, title TEXT, summary TEXT,
  centroid BLOB, member_count INTEGER NOT NULL DEFAULT 0,
  tier TEXT NOT NULL DEFAULT 'active', status TEXT NOT NULL DEFAULT 'active',
  heat REAL NOT NULL DEFAULT 0.0, importance REAL NOT NULL DEFAULT 0.0,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, metadata TEXT
);
CREATE TABLE synapses (
  synapse_id INTEGER PRIMARY KEY AUTOINCREMENT, from_neuron TEXT NOT NULL,
  to_neuron TEXT NOT NULL, weight REAL NOT NULL DEFAULT 0.0,
  base_weight REAL NOT NULL DEFAULT 0.0, relation TEXT NOT NULL DEFAULT '',
  directed INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'active',
  last_activated_at TEXT, activation_count INTEGER NOT NULL DEFAULT 0,
  decay_rate REAL NOT NULL DEFAULT 0.0, reinforce_rate REAL NOT NULL DEFAULT 0.0,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, metadata TEXT,
  UNIQUE(from_neuron, to_neuron)
);
CREATE TABLE memory_activation (
  node_id TEXT PRIMARY KEY, strength REAL NOT NULL DEFAULT 0.0,
  tier TEXT NOT NULL DEFAULT 'active', last_activated_at TEXT,
  activation_count INTEGER NOT NULL DEFAULT 0, tier_changed_at TEXT,
  purge_after TEXT, metadata TEXT
);
CREATE TABLE memory_edges (
  edge_id INTEGER PRIMARY KEY AUTOINCREMENT, from_id TEXT NOT NULL,
  to_id TEXT NOT NULL, type TEXT NOT NULL, weight REAL DEFAULT 1.0,
  created_at TEXT NOT NULL, metadata TEXT, UNIQUE(from_id, to_id, type)
);
"""


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _SchemaCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self._tmp.name, "graph.sqlite")

    def tearDown(self):
        self._tmp.cleanup()

    def make_v1(self):
        with sqlite3.connect(self.db) as connection:
            connection.executescript(V1_SCHEMA)
            connection.execute("PRAGMA user_version=1")

    def columns(self, table):
        with sqlite3.connect(self.db) as connection:
            return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}

    def version(self):
        with sqlite3.connect(self.db) as connection:
            return int(connection.execute("PRAGMA user_version").fetchone()[0])


class MigrationTests(_SchemaCase):
    def test_v1_database_gains_v3_columns_and_tables(self):
        self.make_v1()
        with store.connect(self.db) as connection:
            added = store.ensure_columns(connection)
        self.assertEqual(added, [])
        self.assertEqual(self.version(), store.SCHEMA_VERSION)
        self.assertTrue(
            {"kind", "sparse_code", "context_key"} <= self.columns("neurons")
        )
        self.assertTrue({"polarity", "stability", "kind"} <= self.columns("synapses"))
        self.assertIn("unstable_until", self.columns("memory_activation"))
        self.assertIn("valid_from", self.columns("memory_edges"))
        with sqlite3.connect(self.db) as connection:
            tables = store.table_names(connection)
        self.assertTrue(
            {"entities", "entity_mentions", "activation_buffer", "replay_runs"}
            <= tables
        )

    def test_second_open_is_a_noop(self):
        self.make_v1()
        with store.connect(self.db):
            pass
        with store.connect(self.db) as connection:
            self.assertEqual(store.ensure_columns(connection), [])

    def test_dry_run_reports_without_writing(self):
        self.make_v1()
        module = _load("memory_brain_migrate", "memory_brain_migrate.py")
        with sqlite3.connect(self.db) as connection:
            missing = module._missing(connection)
            tables = module._missing_tables(connection)
        self.assertIn("kind", missing["neurons"])
        self.assertIn("entities", tables)
        self.assertEqual(self.version(), 1)

    def test_rollback_restores_newest_backup(self):
        self.make_v1()
        module = _load("memory_brain_migrate", "memory_brain_migrate.py")
        with store.connect(self.db):
            pass
        self.assertEqual(self.version(), store.SCHEMA_VERSION)
        backup = Path(self.db).with_name("graph.v1-20200101000000.sqlite")
        with sqlite3.connect(backup) as connection:
            connection.executescript(V1_SCHEMA)
            connection.execute("PRAGMA user_version=1")
        result = module._rollback(Path(self.db))
        self.assertEqual(result["restored"], str(backup))
        self.assertEqual(self.version(), 1)

    def test_rollback_without_backup_refuses(self):
        self.make_v1()
        module = _load("memory_brain_migrate", "memory_brain_migrate.py")
        with self.assertRaises(SystemExit):
            module._rollback(Path(self.db))


class OldExportImportTests(_SchemaCase):
    def test_v2_export_imports_into_v3(self):
        module = _load("memory_network_rollback", "memory_network_rollback.py")
        self.make_v1()
        with store.connect(self.db):
            pass
        now = "2026-06-01T00:00:00"
        payload = {
            "neurons": [
                {
                    "neuron_id": "ep1",
                    "title": "old",
                    "member_count": 1,
                    "tier": "active",
                    "status": "active",
                    "created_at": now,
                    "updated_at": now,
                    "metadata": "{}",
                }
            ],
            "synapses": [],
        }
        source = Path(self._tmp.name) / "network.json"
        source.write_text(json.dumps(payload), encoding="utf-8")
        restored = module._import(self.db, source)
        self.assertEqual(restored["tables"]["neurons"], 1)
        with sqlite3.connect(self.db) as connection:
            row = connection.execute(
                "SELECT kind, sparse_code FROM neurons WHERE neuron_id='ep1'"
            ).fetchone()
            buffers = connection.execute(
                "SELECT COUNT(*) FROM activation_buffer"
            ).fetchone()[0]
        self.assertEqual(row[0], "episodic")
        self.assertIsNone(row[1])
        self.assertEqual(buffers, 0)


if __name__ == "__main__":
    unittest.main()
