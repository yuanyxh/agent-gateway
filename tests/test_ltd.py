"""Tests for negative feedback (LTD)."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QDRANT_PATH", tempfile.mkdtemp(prefix="agent-gateway-test-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_gateway.services.memory import strength, store  # noqa: E402


class LtdTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self._tmp.name, "graph.sqlite")
        now = "2026-06-01T00:00:00"
        with store.connect(self.db) as connection:
            for name in ("a", "b"):
                connection.execute(
                    "INSERT INTO neurons (neuron_id, member_count, tier, status, "
                    "created_at, updated_at) VALUES (?, 1, 'active', 'active', ?, ?)",
                    (name, now, now),
                )
            connection.execute(
                "INSERT INTO neuron_members (neuron_id, memory_id, layer, weight, "
                "role, joined_at) VALUES ('a', 'm1', 'hot', 1.0, 'member', ?)",
                (now,),
            )
            connection.execute(
                "INSERT INTO synapses (from_neuron, to_neuron, weight, base_weight, "
                "relation, created_at, updated_at) VALUES ('a', 'b', 0.8, 0.8, "
                "'same_task', ?, ?)",
                (now, now),
            )
            connection.execute(
                "INSERT INTO memory_activation (node_id, strength, tier, stability) "
                "VALUES ('m1', 0.5, 'active', 40.0)"
            )

    def tearDown(self):
        self._tmp.cleanup()

    def weight(self):
        with store.connect(self.db) as connection:
            return connection.execute(
                "SELECT weight FROM synapses WHERE from_neuron='a'"
            ).fetchone()[0]

    def stability(self):
        with store.connect(self.db) as connection:
            return connection.execute(
                "SELECT stability FROM memory_activation WHERE node_id='m1'"
            ).fetchone()[0]

    def test_unknown_memory_changes_nothing(self):
        result = strength.apply_ltd("missing", db_path=self.db)
        self.assertEqual(result["synapses"], 0)
        self.assertAlmostEqual(self.weight(), 0.8)

    def test_weakening_lowers_weight_and_stability(self):
        result = strength.apply_ltd("m1", db_path=self.db)
        self.assertEqual(result["synapses"], 1)
        self.assertAlmostEqual(self.weight(), 0.75, places=3)
        self.assertAlmostEqual(self.stability(), 20.0, places=3)

    def test_weight_never_goes_negative(self):
        for _ in range(40):
            strength.apply_ltd("m1", delta=0.1, db_path=self.db)
        self.assertGreaterEqual(self.weight(), 0.0)

    def test_stability_has_floor(self):
        for _ in range(20):
            strength.apply_ltd("m1", db_path=self.db)
        self.assertGreaterEqual(self.stability(), strength.STABILITY_FLOOR)


if __name__ == "__main__":
    unittest.main()
