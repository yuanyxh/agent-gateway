"""Tests for the working-memory activation buffer."""

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

os.environ.setdefault("QDRANT_PATH", tempfile.mkdtemp(prefix="agent-gateway-test-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_gateway.services.memory import buffer, store  # noqa: E402


class BufferTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self._tmp.name, "graph.sqlite")
        self.now = datetime(2026, 11, 11, 12, 0, 0)

    def tearDown(self):
        self._tmp.cleanup()

    def test_write_then_read(self):
        written = buffer.writeback(
            [{"neuron_id": "n1", "activation": 0.8}], now=self.now, db_path=self.db
        )
        self.assertEqual(written, 1)
        self.assertAlmostEqual(buffer.read(now=self.now, db_path=self.db)["n1"], 0.8)

    def test_values_decay_every_five_minutes(self):
        buffer.writeback(
            [{"neuron_id": "n1", "activation": 1.0}], now=self.now, db_path=self.db
        )
        later = self.now + timedelta(minutes=10)
        value = buffer.read(now=later, db_path=self.db)["n1"]
        self.assertAlmostEqual(value, 0.81, places=3)

    def test_expired_entries_are_gone(self):
        buffer.writeback(
            [{"neuron_id": "n1", "activation": 1.0}], now=self.now, db_path=self.db
        )
        later = self.now + timedelta(minutes=31)
        self.assertEqual(buffer.read(now=later, db_path=self.db), {})

    def test_rows_are_capped(self):
        items = [
            {"neuron_id": f"n{index}", "activation": 0.5 + index / 1000}
            for index in range(250)
        ]
        buffer.writeback(items, now=self.now, db_path=self.db)
        with store.connect(self.db) as connection:
            total = connection.execute(
                "SELECT COUNT(*) FROM activation_buffer"
            ).fetchone()[0]
        self.assertLessEqual(total, buffer.MAX_ROWS)
        self.assertEqual(
            len(buffer.read(now=self.now, db_path=self.db)), buffer.MAX_ROWS
        )

    def test_empty_items_do_nothing(self):
        self.assertEqual(buffer.writeback([], now=self.now, db_path=self.db), 0)


if __name__ == "__main__":
    unittest.main()
