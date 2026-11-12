"""Tests for inferred synapses: writing, review and expiry."""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

os.environ.setdefault("QDRANT_PATH", tempfile.mkdtemp(prefix="agent-gateway-test-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_gateway.services.memory import replay, store  # noqa: E402


class InferenceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self._tmp.name, "graph.sqlite")
        self.now = datetime(2026, 11, 11, 12, 0, 0)

    def tearDown(self):
        self._tmp.cleanup()

    def rows(self):
        with store.connect(self.db) as connection:
            return connection.execute(
                "SELECT synapse_id, weight, inferred, status, metadata FROM synapses"
            ).fetchall()

    def test_inference_is_low_weight_and_traceable(self):
        action = replay.write_inference(
            "b", "a", run_id="rp_1", reason="shared meal", db_path=self.db, now=self.now
        )
        self.assertEqual(action, "inserted")
        row = self.rows()[0]
        self.assertAlmostEqual(row[1], replay.INFER_WEIGHT)
        self.assertEqual(row[2], 1)
        metadata = json.loads(row[4])
        self.assertEqual(metadata["run_id"], "rp_1")

    def test_duplicate_inference_is_ignored(self):
        replay.write_inference("a", "b", run_id="rp_1", db_path=self.db, now=self.now)
        action = replay.write_inference(
            "b", "a", run_id="rp_2", db_path=self.db, now=self.now
        )
        self.assertEqual(action, "exists")
        self.assertEqual(len(self.rows()), 1)

    def test_accept_promotes_edge(self):
        replay.write_inference("a", "b", run_id="rp_1", db_path=self.db, now=self.now)
        synapse_id = self.rows()[0][0]
        self.assertTrue(
            replay.decide_inference(synapse_id, accept=True, db_path=self.db)
        )
        row = self.rows()[0]
        self.assertEqual(row[2], 0)
        self.assertAlmostEqual(row[1], replay.INFER_ACCEPT_WEIGHT)

    def test_reject_archives_edge(self):
        replay.write_inference("a", "b", run_id="rp_1", db_path=self.db, now=self.now)
        synapse_id = self.rows()[0][0]
        self.assertTrue(
            replay.decide_inference(synapse_id, accept=False, db_path=self.db)
        )
        self.assertEqual(self.rows()[0][3], "archived")

    def test_expiry_archives_old_inferences(self):
        old = self.now - timedelta(days=30)
        replay.write_inference("a", "b", run_id="rp_1", db_path=self.db, now=old)
        expired = replay.expire_inferences(days=14, db_path=self.db, now=self.now)
        self.assertEqual(expired, 1)
        self.assertEqual(self.rows()[0][3], "archived")


if __name__ == "__main__":
    unittest.main()
