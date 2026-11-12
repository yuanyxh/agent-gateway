"""Tests for sparse engram codes and their storage round-trip."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QDRANT_PATH", tempfile.mkdtemp(prefix="agent-gateway-test-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import numpy as np  # noqa: E402

from agent_gateway.services.memory import network, sparse, store  # noqa: E402


class SparseCodeTests(unittest.TestCase):
    def setUp(self):
        self.rng = np.random.default_rng(3)

    def test_encode_is_deterministic(self):
        vector = self.rng.standard_normal(768)
        self.assertEqual(sparse.encode(vector), sparse.encode(vector))

    def test_empty_vector_is_rejected(self):
        with self.assertRaises(ValueError):
            sparse.encode([])

    def test_unrelated_vectors_stay_separate(self):
        left, right = self.rng.standard_normal((2, 768))
        overlap = sparse.jaccard(
            sparse.decode(sparse.encode(left)), sparse.decode(sparse.encode(right))
        )
        self.assertLess(overlap, 0.15)

    def test_similar_vectors_overlap(self):
        base = self.rng.standard_normal(768)
        near = base + 0.1 * self.rng.standard_normal(768)
        overlap = sparse.jaccard(
            sparse.decode(sparse.encode(base)), sparse.decode(sparse.encode(near))
        )
        self.assertGreater(overlap, 0.3)

    def test_shared_context_adds_overlap(self):
        left, right = self.rng.standard_normal((2, 768))
        key = sparse.context_key("ep1", "chat", "2026-06-03T10:00:00")
        code_left = sparse.decode(sparse.encode(left, key))
        code_right = sparse.decode(sparse.encode(right, key))
        self.assertGreater(sparse.jaccard(code_left, code_right), 0.1)

    def test_context_key_uses_month_bucket(self):
        first = sparse.context_key("ep1", "chat", "2026-06-03T10:00:00")
        second = sparse.context_key("ep1", "chat", "2026-06-28T23:00:00")
        third = sparse.context_key("ep1", "chat", "2026-07-01T00:00:00")
        self.assertEqual(first, second)
        self.assertNotEqual(first, third)

    def test_code_length_matches_dimensions(self):
        code = sparse.encode(self.rng.standard_normal(768))
        self.assertEqual(len(code), sparse.DIMS // 8)


class SparseStorageTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self._tmp.name, "graph.sqlite")

    def tearDown(self):
        self._tmp.cleanup()

    def test_round_trip(self):
        now = "2026-06-01T00:00:00"
        with store.connect(self.db) as connection:
            connection.execute(
                "INSERT INTO neurons (neuron_id, member_count, tier, status, "
                "created_at, updated_at) VALUES ('ep1', 1, 'active', 'active', ?, ?)",
                (now, now),
            )
        code = sparse.encode(np.random.default_rng(5).standard_normal(768), "ctx")
        self.assertTrue(network.set_neuron_sparse("ep1", code, "ctx", db_path=self.db))
        stored, context = network.neuron_sparse("ep1", db_path=self.db)
        self.assertEqual(stored, code)
        self.assertEqual(context, "ctx")

    def test_missing_neuron_returns_empty(self):
        stored, context = network.neuron_sparse("nope", db_path=self.db)
        self.assertIsNone(stored)
        self.assertEqual(context, "")


if __name__ == "__main__":
    unittest.main()
