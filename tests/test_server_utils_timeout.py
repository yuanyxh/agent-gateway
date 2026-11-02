"""`run_with_timeout` bounds a blocking call without breaking normal results."""

from __future__ import annotations

import asyncio
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_gateway.services.mcp.server_utils import run_with_timeout  # noqa: E402


def _slow(delay: float, value: str) -> str:
    time.sleep(delay)
    return value


class RunWithTimeoutTest(unittest.TestCase):
    def test_returns_the_result_when_inside_budget(self):
        result, timed_out = asyncio.run(
            run_with_timeout(_slow, 0.01, "ok", timeout=1.0)
        )
        self.assertEqual(result, "ok")
        self.assertFalse(timed_out)

    def test_reports_timeout_and_discards_the_result(self):
        result, timed_out = asyncio.run(
            run_with_timeout(_slow, 0.2, "late", timeout=0.01)
        )
        self.assertIsNone(result)
        self.assertTrue(timed_out)


if __name__ == "__main__":
    unittest.main()
