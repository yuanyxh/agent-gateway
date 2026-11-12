"""Tests for the inhibitory attractor and its chain reach."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QDRANT_PATH", tempfile.mkdtemp(prefix="agent-gateway-test-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_gateway.services.memory import attractor, store  # noqa: E402


class _AttractorCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self._tmp.name, "graph.sqlite")
        now = "2026-06-01T00:00:00"
        with store.connect(self.db) as connection:
            for name in ("a", "b", "c", "d", "e"):
                connection.execute(
                    "INSERT OR IGNORE INTO neurons (neuron_id, member_count, tier, "
                    "status, created_at, updated_at) VALUES (?, 1, 'active', "
                    "'active', ?, ?)",
                    (name, now, now),
                )

    def tearDown(self):
        self._tmp.cleanup()

    def edge(self, left, right, weight=0.9, polarity=1):
        now = "2026-06-01T00:00:00"
        with store.connect(self.db) as connection:
            connection.execute(
                "INSERT OR REPLACE INTO synapses (from_neuron, to_neuron, weight, "
                "base_weight, relation, polarity, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'same_task', ?, ?, ?)",
                (left, right, weight, weight, polarity, now, now),
            )

    def chain(self):
        for left, right in (("a", "b"), ("b", "c"), ("c", "d"), ("d", "e")):
            self.edge(left, right)


class ReachTests(_AttractorCase):
    def test_five_hop_chain_stays_alive(self):
        self.chain()
        result = attractor.activate({"a": 1.0}, db_path=self.db, hub_degree=99)
        reached = {
            item["neuron_id"]: item["activation"] for item in result["activated"]
        }
        self.assertGreater(reached.get("e", 0.0), 0.0)

    def test_hub_restart_keeps_value_high(self):
        self.chain()
        result = attractor.activate(
            {"a": 1.0}, db_path=self.db, hub_degree=1, restart=0.6
        )
        reached = {
            item["neuron_id"]: item["activation"] for item in result["activated"]
        }
        self.assertGreaterEqual(reached["d"], 0.6)

    def test_inhibitory_edge_lowers_activation(self):
        self.edge("a", "b", polarity=-1)
        self.edge("a", "c", polarity=1)
        result = attractor.activate({"a": 1.0}, db_path=self.db, hub_degree=99)
        reached = {
            item["neuron_id"]: item["activation"] for item in result["activated"]
        }
        self.assertLess(reached.get("b", 0.0), reached.get("c", 0.0))

    def test_empty_seeds_return_empty(self):
        result = attractor.activate({}, db_path=self.db)
        self.assertEqual(result["activated"], [])
        self.assertTrue(result["converged"])

    def test_budget_exhausted_reports(self):
        self.chain()
        result = attractor.activate(
            {"a": 1.0}, db_path=self.db, max_active=1, steps=3, hub_degree=99
        )
        self.assertTrue(result["budget_exhausted"])


if __name__ == "__main__":
    unittest.main()
