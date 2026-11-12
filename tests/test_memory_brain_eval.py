"""Tests for the memory-brain evaluation baseline."""

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QDRANT_PATH", tempfile.mkdtemp(prefix="agent-gateway-test-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_gateway.services.memory import store  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location(
        "memory_brain_eval", ROOT / "scripts" / "memory_brain_eval.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _EvalCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self._tmp.name, "graph.sqlite")

    def tearDown(self):
        self._tmp.cleanup()

    def neuron(self, neuron_id, created="2026-06-01T00:00:00"):
        with store.connect(self.db) as connection:
            connection.execute(
                "INSERT OR IGNORE INTO neurons (neuron_id, member_count, tier, "
                "status, created_at, updated_at) VALUES (?, 1, 'active', 'active', ?, ?)",
                (neuron_id, created, created),
            )
            connection.execute(
                "INSERT OR IGNORE INTO memory_nodes (node_id, layer, status, "
                "content_hash, title, created_at, updated_at) VALUES (?, 'hot', "
                "'active', ?, ?, ?, ?)",
                (f"m-{neuron_id}", neuron_id, neuron_id, created, created),
            )
            connection.execute(
                "INSERT OR IGNORE INTO neuron_members (neuron_id, memory_id, layer, "
                "weight, role, joined_at) VALUES (?, ?, 'hot', 1.0, 'member', ?)",
                (neuron_id, f"m-{neuron_id}", created),
            )

    def edge(self, left, right):
        now = "2026-06-01T00:00:00"
        with store.connect(self.db) as connection:
            connection.execute(
                "INSERT OR IGNORE INTO synapses (from_neuron, to_neuron, weight, "
                "base_weight, relation, created_at, updated_at) VALUES (?, ?, 0.8, "
                "0.8, 'same_task', ?, ?)",
                (left, right, now, now),
            )


class TraceMetricsTests(_EvalCase):
    def test_empty_trace_reports_empty(self):
        module = _load()
        with store.connect(self.db) as connection:
            result = module.collect([], connection)
        self.assertEqual(result["trace"]["status"], "empty")

    def test_trace_metrics_are_deterministic(self):
        module = _load()
        rows = [
            {"status": "ok", "count": 5, "latency_ms": {"total": 10.0}},
            {"status": "no_match", "count": 0, "latency_ms": {"total": 30.0}},
            {"status": "ok", "count": 3, "latency_ms": {"total": 20.0}},
        ]
        with store.connect(self.db) as connection:
            first = module.collect(rows, connection)
            second = module.collect(rows, connection)
        self.assertEqual(first, second)
        self.assertEqual(first["trace"]["calls"], 3)
        self.assertEqual(first["trace"]["status_counts"]["ok"], 2)
        self.assertEqual(first["trace"]["latency_p95_ms"], 30.0)


class TopologyTests(_EvalCase):
    def test_isolated_and_components(self):
        module = _load()
        for name in ("a", "b", "c", "d"):
            self.neuron(name)
        self.edge("a", "b")
        self.edge("b", "c")
        with store.connect(self.db) as connection:
            result = module.topology(connection)
        self.assertEqual(result["neurons"], 4)
        self.assertEqual(result["edges"], 2)
        self.assertEqual(result["isolated"], 1)
        self.assertAlmostEqual(result["isolated_ratio"], 0.25)
        self.assertEqual(result["components"], [3])
        self.assertEqual(result["diameter"], 2)

    def test_empty_graph_is_zeroed(self):
        module = _load()
        with store.connect(self.db) as connection:
            result = module.topology(connection)
        self.assertEqual(result["neurons"], 0)
        self.assertEqual(result["isolated_ratio"], 0.0)
        self.assertEqual(result["components"], [])
        self.assertEqual(result["diameter"], 0)


class SamplingTests(_EvalCase):
    def test_sample_is_reproducible_and_keeps_oldest(self):
        module = _load()
        for index in range(6):
            self.neuron(f"old{index}", created="2026-06-01T00:00:00")
        for index in range(20):
            self.neuron(f"new{index}", created="2026-10-01T00:00:00")
        with store.connect(self.db) as connection:
            first = module.sample_neurons(connection, size=8, seed=7, per_stratum=1)
            second = module.sample_neurons(connection, size=8, seed=7, per_stratum=1)
        self.assertEqual(first["neurons"], second["neurons"])
        self.assertEqual(
            {name for name in first["neurons"] if name.startswith("old")},
            {f"old{index}" for index in range(6)},
        )
        self.assertIn("2026-06", first["strata_covered"])


if __name__ == "__main__":
    unittest.main()
