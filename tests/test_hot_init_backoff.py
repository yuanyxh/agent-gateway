"""Backoff scheduling and diagnostics for hot-layer initialization."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QDRANT_PATH", tempfile.mkdtemp(prefix="agent-gateway-test-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_gateway.services.mcp import server_memory  # noqa: E402


class HotRetryDelayTest(unittest.TestCase):
    def test_delay_doubles_and_caps(self):
        delays = [server_memory._hot_retry_delay(i) for i in range(5)]

        self.assertEqual(delays, [0.5, 1.0, 2.0, 4.0, 8.0])


class HotInitDiagnosticsTest(unittest.TestCase):
    def test_failure_logs_qdrant_path(self):
        with (
            mock.patch.object(server_memory, "Memory") as memory,
            mock.patch.object(server_memory.time, "sleep"),
            mock.patch.object(server_memory.logger, "error") as error,
        ):
            memory.from_config.side_effect = RuntimeError("lock held")
            ok = server_memory._init_hot_memory()

        self.assertFalse(ok)
        self.assertIn(server_memory.MEM0_QDRANT_PATH, error.call_args.args[0])
