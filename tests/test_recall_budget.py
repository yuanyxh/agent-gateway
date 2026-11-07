"""Tests for the recall character budget and one-hop edge expansion."""

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QDRANT_PATH", tempfile.mkdtemp(prefix="agent-gateway-test-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_gateway.services.mcp import server_memory  # noqa: E402
from agent_gateway.services.retrieval.arms import recall as recall_arm  # noqa: E402
from agent_gateway.services.retrieval.types import RetrievedItem  # noqa: E402


def _out(results, related):
    return {
        "status": "ok",
        "results": results,
        "related": related,
        "count": len(results),
        "related_count": len(related),
        "trace_id": "t1",
    }


class RecallBudgetTest(unittest.TestCase):
    def _run(self, out, arguments):
        pipeline = mock.Mock()
        pipeline.run_query.return_value = out
        with (
            mock.patch.object(
                server_memory, "_build_recall_pipeline", return_value=pipeline
            ),
            mock.patch.object(
                server_memory, "ensure_hot_memory", mock.AsyncMock(return_value=True)
            ),
        ):
            result = asyncio.run(server_memory._handle_recall(arguments))
        return json.loads(result[0].text)

    def test_truncated_primary_results_drop_related(self):
        out = _out(
            [{"id": "a", "content": "x" * 2000}, {"id": "b", "content": "short"}],
            [{"id": "r", "content": "related"}],
        )

        data = self._run(out, {"keywords": ["q"], "max_chars": 600})

        self.assertEqual(len(data["results"]), 1)
        self.assertTrue(data["results"][0]["content_truncated"])
        self.assertEqual(data["related"], [])

    def test_related_rows_use_the_remaining_budget(self):
        out = _out(
            [{"id": "a", "content": "short"}],
            [{"id": "r", "content": "y" * 2000}],
        )

        data = self._run(out, {"keywords": ["q"], "max_chars": 800})

        self.assertEqual(data["results"][0]["id"], "a")
        self.assertEqual(len(data["related"]), 1)
        self.assertTrue(data["related"][0]["content_truncated"])
        self.assertEqual(data["trace_id"], "t1")

    def test_zero_disables_the_budget(self):
        out = _out(
            [{"id": "a", "content": "x" * 5000}],
            [{"id": "r", "content": "y" * 5000}],
        )

        data = self._run(out, {"keywords": ["q"], "max_chars": 0})

        self.assertFalse(data["results"][0].get("content_truncated"))
        self.assertEqual(len(data["related"]), 1)

    def test_primary_order_is_unchanged(self):
        out = _out(
            [
                {"id": "first", "content": "a" * 100},
                {"id": "second", "content": "b" * 100},
                {"id": "third", "content": "c" * 100},
            ],
            [],
        )

        data = self._run(out, {"keywords": ["q"], "max_chars": 100000})

        self.assertEqual(
            [row["id"] for row in data["results"]], ["first", "second", "third"]
        )


class EdgeExpansionTest(unittest.TestCase):
    def test_neighbour_is_labelled_with_its_edge_type(self):
        item = RetrievedItem(
            id="h1", kind="memory", score=0.8, score_kind="dense", layer="hot"
        )
        edge = {"from_id": "h2", "to_id": "h1", "type": "supersedes"}

        with (
            mock.patch.object(
                recall_arm.memory_graph, "neighbors", return_value=[edge]
            ),
            mock.patch.object(
                recall_arm.memory_store,
                "get_nodes",
                return_value={"h2": {"node_id": "h2", "layer": "hot", "metadata": {}}},
            ),
            mock.patch.object(
                recall_arm,
                "_hot_points",
                return_value={
                    "h2": {
                        "vector": [1.0, 0.0],
                        "content": "previous version",
                        "heat_score": 1.0,
                        "episode_id": "",
                    }
                },
            ),
            mock.patch.object(recall_arm, "_cold_points", return_value={}),
        ):
            expanded = recall_arm._expand_edges([item], [1.0, 0.0], 0.5)

        self.assertEqual(len(expanded), 1)
        self.assertEqual(expanded[0].id, "h2")
        self.assertEqual(expanded[0].extra["edge"], "supersedes")
        self.assertEqual(expanded[0].content, "previous version")

    def test_below_floor_neighbours_are_dropped(self):
        item = RetrievedItem(
            id="h1", kind="memory", score=0.8, score_kind="dense", layer="hot"
        )
        edge = {"from_id": "h1", "to_id": "h2", "type": "relates"}

        with (
            mock.patch.object(
                recall_arm.memory_graph, "neighbors", return_value=[edge]
            ),
            mock.patch.object(
                recall_arm.memory_store,
                "get_nodes",
                return_value={"h2": {"node_id": "h2", "layer": "hot", "metadata": {}}},
            ),
            mock.patch.object(
                recall_arm,
                "_hot_points",
                return_value={
                    "h2": {
                        "vector": [0.0, 1.0],
                        "content": "unrelated",
                        "heat_score": 1.0,
                        "episode_id": "",
                    }
                },
            ),
            mock.patch.object(recall_arm, "_cold_points", return_value={}),
        ):
            expanded = recall_arm._expand_edges([item], [1.0, 0.0], 0.5)

        self.assertEqual(expanded, [])

    def test_missing_query_vector_keeps_the_primary_order(self):
        item = RetrievedItem(
            id="h1", kind="memory", score=0.8, score_kind="dense", layer="hot"
        )

        self.assertEqual(recall_arm._expand_edges([item], None, 0.5), [])


if __name__ == "__main__":
    unittest.main()
