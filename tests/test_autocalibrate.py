"""Calibration probe sampling stays uniform and bounded."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QDRANT_PATH", tempfile.mkdtemp(prefix="agent-gateway-test-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_gateway.services.retrieval import autocalibrate  # noqa: E402


class SampleProbesTest(unittest.TestCase):
    def test_keeps_everything_below_cap(self):
        items = [(str(i), "text") for i in range(10)]
        self.assertEqual(autocalibrate.sample_probes(items, 10), items)

    def test_returns_cap_and_covers_tail(self):
        items = [(str(i), "text") for i in range(100)]
        sampled = autocalibrate.sample_probes(items, 10)
        self.assertEqual(len(sampled), 10)
        self.assertTrue(any(int(node_id) >= 50 for node_id, _ in sampled))
        self.assertEqual(autocalibrate.sample_probes(items, 10), sampled)


if __name__ == "__main__":
    unittest.main()
