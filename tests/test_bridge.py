"""Tests for small-world bridge edges between disconnected components."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QDRANT_PATH", tempfile.mkdtemp(prefix="agent-gateway-test-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import numpy as np  # noqa: E402

from agent_gateway.services.memory import network, network_build, store  # noqa: E402


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self._tmp.name, "graph.sqlite")
        now = "2026-06-01T00:00:00"
        vectors = {
            "a": [1.0, 0.0, 0.0],
            "b": [0.0, 1.0, 0.0],
            "c": [0.98, 0.02, 0.0],
            "d": [0.0, 0.0, 1.0],
        }
        with store.connect(self.db) as connection:
            for neuron_id, vector in vectors.items():
                connection.execute(
                    "INSERT INTO neurons (neuron_id, member_count, tier, status, "
                    "centroid, created_at, updated_at) VALUES (?, 1, 'active', "
                    "'active', ?, ?, ?)",
                    (
                        neuron_id,
                        np.asarray(vector, dtype=np.float32).tobytes(),
                        now,
                        now,
                    ),
                )
        network.upsert_synapse(
            {"from_neuron": "a", "to_neuron": "b", "weight": 0.9, "relation": "x"},
            db_path=self.db,
        )
        network.upsert_synapse(
            {"from_neuron": "c", "to_neuron": "d", "weight": 0.9, "relation": "x"},
            db_path=self.db,
        )

    def tearDown(self):
        self._tmp.cleanup()

    def bridges(self):
        with store.connect(self.db) as connection:
            return connection.execute(
                "SELECT from_neuron, to_neuron, weight, kind FROM synapses "
                "WHERE kind='bridge'"
            ).fetchall()

    def test_connects_small_component_to_main(self):
        proposed = network_build.bridge_components(self.db)
        self.assertEqual(len(proposed), 1)
        self.assertEqual(
            {proposed[0]["left_group"], proposed[0]["right_group"]}, {"a", "c"}
        )
        written = network_build.write_bridges(proposed, db_path=self.db)
        self.assertEqual(written, 1)
        row = self.bridges()[0]
        self.assertEqual(row[3], "bridge")
        self.assertAlmostEqual(row[2], 1.0, places=2)

    def test_write_is_idempotent(self):
        proposed = network_build.bridge_components(self.db)
        network_build.write_bridges(proposed, db_path=self.db)
        self.assertEqual(network_build.write_bridges(proposed, db_path=self.db), 0)
        self.assertEqual(len(self.bridges()), 1)

    def test_archived_bridge_is_revived(self):
        proposed = network_build.bridge_components(self.db)
        network_build.write_bridges(proposed, db_path=self.db)
        with store.connect(self.db) as connection:
            connection.execute(
                "UPDATE synapses SET status='archived' WHERE kind='bridge'"
            )
        self.assertEqual(network_build.write_bridges(proposed, db_path=self.db), 1)
        with store.connect(self.db) as connection:
            status = connection.execute(
                "SELECT status FROM synapses WHERE kind='bridge'"
            ).fetchone()[0]
        self.assertEqual(status, "active")

    def test_single_component_needs_no_bridge(self):
        network.upsert_synapse(
            {"from_neuron": "b", "to_neuron": "c", "weight": 0.9, "relation": "x"},
            db_path=self.db,
        )
        self.assertEqual(network_build.bridge_components(self.db), [])


if __name__ == "__main__":
    unittest.main()
