"""Tests for FSRS-style forgetting and per-row decay."""

import math
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

os.environ.setdefault("QDRANT_PATH", tempfile.mkdtemp(prefix="agent-gateway-test-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_gateway.core import config  # noqa: E402
from agent_gateway.services.memory import forgetting, network, strength, store  # noqa: E402


class ForgettingMathTests(unittest.TestCase):
    def test_retrievability_at_stability_is_ninety_percent(self):
        self.assertAlmostEqual(forgetting.retrievability(30, 30), 0.9, places=6)
        self.assertAlmostEqual(forgetting.retrievability(7, 7), 0.9, places=6)

    def test_low_retrievability_grows_more(self):
        low = forgetting.next_stability(30, 5, 0.4)
        high = forgetting.next_stability(30, 5, 0.95)
        self.assertGreater(low, high)

    def test_initial_stability_tracks_importance(self):
        self.assertLess(
            forgetting.initial_stability(1), forgetting.initial_stability(9)
        )

    def test_higher_stability_decays_slower(self):
        self.assertLess(
            forgetting.retrievability(10, 10), forgetting.retrievability(100, 10)
        )


class DecayTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self._tmp.name, "graph.sqlite")
        self.now = datetime(2026, 11, 11, 12, 0, 0)
        with store.connect(self.db) as connection:
            connection.executemany(
                "INSERT INTO memory_activation (node_id, strength, tier, stability, "
                "last_activated_at) VALUES (?, 1.0, 'active', ?, ?)",
                [
                    ("short", 10.0, (self.now - timedelta(days=10)).isoformat()),
                    ("long", 100.0, (self.now - timedelta(days=10)).isoformat()),
                ],
            )
        network.set_meta(
            "last_decay_at",
            (self.now - timedelta(days=10)).isoformat(),
            db_path=self.db,
        )

    def tearDown(self):
        self._tmp.cleanup()

    def strengths(self):
        with store.connect(self.db) as connection:
            return dict(
                connection.execute(
                    "SELECT node_id, strength FROM memory_activation"
                ).fetchall()
            )

    def test_per_row_decay_uses_stability(self):
        result = forgetting.apply_decay_v3(now=self.now, db_path=self.db)
        self.assertEqual(result["rows"], 2)
        values = self.strengths()
        self.assertLess(values["short"], values["long"])
        self.assertAlmostEqual(values["short"], 0.9, places=3)

    def test_switch_off_keeps_old_half_life(self):
        with mock.patch.object(config, "memory_brain_forgetting", return_value=False):
            result = strength.apply_decay(now=self.now, db_path=self.db)
        self.assertEqual(result["memory_rows"], 2)
        expected = math.exp(-math.log(2) * 10 / strength.STRENGTH_HALF_LIFE_DAYS)
        self.assertAlmostEqual(self.strengths()["short"], expected, places=3)

    def test_recall_grows_stability(self):
        before = self._stability("short")
        strength.reinforce_memory("short", now=self.now, db_path=self.db)
        self.assertGreater(self._stability("short"), before)

    def _stability(self, node_id):
        with store.connect(self.db) as connection:
            return connection.execute(
                "SELECT stability FROM memory_activation WHERE node_id=?", (node_id,)
            ).fetchone()[0]


if __name__ == "__main__":
    unittest.main()
