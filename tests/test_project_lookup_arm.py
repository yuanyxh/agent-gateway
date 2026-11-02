"""Tests for the project-lookup retrieval arm."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QDRANT_PATH", tempfile.mkdtemp(prefix="agent-gateway-test-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_gateway.services.retrieval.arms.project_lookup import (  # noqa: E402
    ProjectLookupArm,
)
from agent_gateway.services.retrieval.types import Query  # noqa: E402

TARGET = "agent_gateway.services.mcp.graph_tools.tool_project_lookup"

ROWS = [
    {
        "name": "scan",
        "kind": "function",
        "file": "a.py",
        "startLine": 10,
        "endLine": 20,
        "callerCount": 3,
        "topCallers": [{"file": "b.py", "symbol": "run"}],
        "resolver": "treesitter",
    },
    {
        "name": "scan",
        "kind": "method",
        "file": "a.py",
        "startLine": 40,
        "endLine": 45,
        "callerCount": 1,
        "topCallers": [],
        "resolver": "treesitter",
    },
]


class ProjectLookupArmTest(unittest.TestCase):
    def _retrieve(self, payload=None, side_effect=None):
        arm = ProjectLookupArm("/tmp/project")
        with mock.patch(TARGET, return_value=payload, side_effect=side_effect):
            return arm.retrieve(Query(text="scan"), 10)

    def test_rows_map_to_ranked_items(self):
        result = self._retrieve({"status": "ok", "results": ROWS})

        self.assertEqual(
            [item.id for item in result.items],
            [
                "project_lookup:a.py:function:scan:10",
                "project_lookup:a.py:method:scan:40",
            ],
        )
        self.assertEqual([item.score for item in result.items], [1.0, 0.5])
        self.assertEqual({item.score_kind for item in result.items}, {"rank"})
        self.assertEqual(result.items[0].locator["end_line"], 20)
        self.assertEqual(result.items[0].extra["caller_count"], 3)
        self.assertEqual(result.items[0].extra["resolver"], "treesitter")

    def test_same_name_in_one_file_keeps_distinct_ids(self):
        result = self._retrieve({"status": "ok", "results": ROWS})

        self.assertNotEqual(result.items[0].id, result.items[1].id)

    def test_lookup_error_becomes_arm_error(self):
        result = self._retrieve(
            {"status": "error", "error": "graph database not found"}
        )

        self.assertEqual(result.items, [])
        self.assertEqual(result.error, "graph database not found")

    def test_exception_is_isolated(self):
        result = self._retrieve(side_effect=RuntimeError("nope"))

        self.assertEqual(result.items, [])
        self.assertEqual(result.error, "RuntimeError: nope")


if __name__ == "__main__":
    unittest.main()
