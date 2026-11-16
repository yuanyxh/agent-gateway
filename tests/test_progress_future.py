"""Progress futures must not leak unretrieved exceptions."""

import concurrent.futures
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QDRANT_PATH", tempfile.mkdtemp(prefix="agent-gateway-test-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_gateway.services.mcp import server  # noqa: E402


class ProgressFutureTest(unittest.TestCase):
    def test_cancelled_future_is_ignored(self):
        future = concurrent.futures.Future()
        future.cancel()

        server._consume_future(future)

        self.assertTrue(future.cancelled())

    def test_failed_future_is_consumed(self):
        future = concurrent.futures.Future()
        future.set_exception(RuntimeError("boom"))

        server._consume_future(future)

        self.assertTrue(future.done())
