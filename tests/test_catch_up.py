"""Tests for catch_up output bounds and pagination."""

import asyncio
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

os.environ.setdefault("QDRANT_PATH", tempfile.mkdtemp(prefix="agent-gateway-test-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_gateway.services.mcp import server_memory  # noqa: E402


def _memories(count: int, content: str = "memory") -> list[dict]:
    now = datetime.now().isoformat()
    return [
        {
            "id": f"m{i}",
            "memory": f"{content}-{i}",
            "created_at": now,
            "metadata": {},
        }
        for i in range(count)
    ]


class CatchUpPaginationTest(unittest.TestCase):
    def test_handler_is_registered(self):
        self.assertIs(
            server_memory.HANDLERS["catch_up"], server_memory._handle_catch_up
        )

    def _run(self, arguments: dict, memories: list[dict]) -> dict:
        hot = mock.Mock()
        hot.get_all.return_value = memories
        with (
            mock.patch.object(
                server_memory, "ensure_hot_memory", mock.AsyncMock(return_value=True)
            ),
            mock.patch.object(server_memory, "get_hot_memory", return_value=hot),
        ):
            result = asyncio.run(server_memory._handle_catch_up(arguments))
        return json.loads(result[0].text)

    def test_limit_and_offset_page_through_results(self):
        data = self._run({"since_days": 1, "limit": 2, "offset": 1}, _memories(5))
        self.assertEqual(data["count"], 5)
        self.assertEqual(data["returned"], 2)
        self.assertEqual(data["offset"], 1)
        self.assertTrue(data["has_more"])
        self.assertEqual(data["next_offset"], 3)

    def test_default_limit_is_bounded(self):
        data = self._run({"since_days": 1}, _memories(60))
        self.assertEqual(data["returned"], 50)
        self.assertTrue(data["truncated"])
        self.assertEqual(data["truncated_by"], "limit")

    def test_character_budget_truncates_single_long_memory(self):
        data = self._run(
            {"since_days": 1, "limit": 1, "max_chars": 600},
            _memories(1, content="x" * 5000),
        )
        self.assertEqual(data["returned"], 1)
        self.assertTrue(data["truncated"])
        self.assertEqual(data["truncated_by"], "chars")
        self.assertTrue(data["results"][0]["content_truncated"])
        self.assertLess(len(data["results"][0]["content"]), 1000)
        self.assertLessEqual(
            len(json.dumps(data["results"][0], ensure_ascii=False)), 600
        )


if __name__ == "__main__":
    unittest.main()
