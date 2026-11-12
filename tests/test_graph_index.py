"""Tests for the CSR graph index and multi-hop expansion."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QDRANT_PATH", tempfile.mkdtemp(prefix="agent-gateway-test-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_gateway.services.memory import graph_index, network, store  # noqa: E402


class _IndexCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self._tmp.name, "graph.sqlite")
        now = "2026-06-01T00:00:00"
        with store.connect(self.db) as connection:
            for name in ("a", "b", "c", "d"):
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

    def build(self):
        return graph_index.GraphIndex.load(self.db)


class ExpandTests(_IndexCase):
    def test_two_hops_reach_third_node(self):
        self.edge("a", "b")
        self.edge("b", "c")
        index = self.build()
        result = index.expand("a", hops=2)
        reached = {item["neuron_id"] for item in result["neurons"]}
        self.assertEqual(reached, {"b", "c"})
        self.assertEqual(result["unknown"], False)

    def test_ten_hops_are_fast(self):
        for left, right in (("a", "b"), ("b", "c"), ("c", "d")):
            self.edge(left, right)
        index = self.build()
        started = __import__("time").perf_counter()
        for _ in range(100):
            index.expand("a", hops=10)
        elapsed = (__import__("time").perf_counter() - started) * 1000 / 100
        self.assertLess(elapsed, 10.0)

    def test_neighbors_match_sql_direction_caps(self):
        for index in range(6):
            self.edge("a", f"out{index}", weight=0.5 + index / 100)
            self.edge(f"in{index}", "a", weight=0.6 + index / 100)
        index = self.build()
        links = index.neighbors("a", limit=8)
        sql = network.synapses_of_many(["a"], limit=8, db_path=self.db)["a"]
        self.assertEqual(
            [link["other"] for link in links], [item["other"] for item in sql]
        )
        self.assertEqual(
            [link["synapse_id"] for link in links],
            [item["synapse_id"] for item in sql],
        )

    def test_unknown_node_is_reported(self):
        index = self.build()
        result = index.expand("missing")
        self.assertTrue(result["unknown"])
        self.assertEqual(result["neurons"], [])

    def test_budget_truncates(self):
        self.edge("a", "b")
        self.edge("a", "c")
        self.edge("a", "d")
        index = self.build()
        result = index.expand("a", hops=1, budget=2)
        self.assertTrue(result["budget_exhausted"])

    def test_inhibitory_edge_is_skipped(self):
        self.edge("a", "b", polarity=-1)
        self.edge("a", "c", polarity=1)
        index = self.build()
        result = index.expand("a", hops=1)
        reached = {item["neuron_id"] for item in result["neurons"]}
        self.assertIn("c", reached)
        self.assertNotIn("b", reached)


if __name__ == "__main__":
    unittest.main()
