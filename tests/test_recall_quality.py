"""Tests for recall relevance floors, related context, and notes matching."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

os.environ.setdefault("QDRANT_PATH", tempfile.mkdtemp(prefix="agent-gateway-test-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_gateway.services.mcp import server_memory  # noqa: E402


class NoteNeedleTest(unittest.TestCase):
    def test_mixed_query_uses_ascii_terms_and_cjk_bigrams(self):
        self.assertEqual(
            server_memory._note_needles("RSS 治理 日报"),
            ["rss", "治理", "日报"],
        )

    def test_chinese_query_has_no_single_char_anchor(self):
        self.assertEqual(
            server_memory._note_needles("最近完成的任务"),
            ["最近", "近完", "完成", "成的", "的任", "任务"],
        )
        self.assertEqual(server_memory._note_needles("中"), [])


class NotesCoverageTest(unittest.TestCase):
    def test_requires_most_terms_not_any_one(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, "full.md").write_text(
                "RSS 治理 日报 规则 改进", encoding="utf-8"
            )
            Path(root, "partial.md").write_text("RSS 日报", encoding="utf-8")
            with (
                mock.patch.object(server_memory, "NOTES_DIR", root),
                mock.patch.object(server_memory, "get_qdrant", return_value=None),
            ):
                results = server_memory.search_cold_layer(
                    "RSS 治理 日报 规则 改进", limit=5
                )

        self.assertEqual([row["id"] for row in results], ["notes:full.md"])
        self.assertAlmostEqual(results[0]["score"], 0.6)


class FakeColdQdrant:
    def __init__(self):
        self.kwargs = None

    def query_points(self, **kwargs):
        self.kwargs = kwargs
        return mock.Mock(points=[])


class ColdThresholdTest(unittest.TestCase):
    def test_query_points_receives_score_threshold(self):
        qdrant = FakeColdQdrant()
        query_vec = mock.Mock()
        query_vec.tolist.return_value = [1.0, 0.0]
        with tempfile.TemporaryDirectory() as root:
            with (
                mock.patch.object(server_memory, "NOTES_DIR", root),
                mock.patch.object(server_memory, "get_qdrant", return_value=qdrant),
                mock.patch.object(server_memory, "embed_query", return_value=query_vec),
            ):
                server_memory.search_cold_layer("q", limit=3)

        self.assertEqual(
            qdrant.kwargs["score_threshold"], server_memory.RECALL_COLD_MIN_SCORE
        )


class FakeColdPoint:
    id = "cold-1"
    score = np.float32(0.8)
    payload = {"content": "cold", "source_file": "x.md"}


class FakeColdQdrantWithPoint:
    def query_points(self, **_kwargs):
        return mock.Mock(points=[FakeColdPoint()])


class ColdScoreSerializationTest(unittest.TestCase):
    def test_cold_score_and_cosine_are_python_floats(self):
        self.assertIsInstance(
            server_memory._cosine(
                np.array([1.0, 0.0], dtype=np.float32),
                np.array([6.0, 8.0], dtype=np.float32),
            ),
            float,
        )
        query_vec = mock.Mock()
        query_vec.tolist.return_value = [1.0, 0.0]
        with tempfile.TemporaryDirectory() as root:
            with (
                mock.patch.object(server_memory, "NOTES_DIR", root),
                mock.patch.object(
                    server_memory,
                    "get_qdrant",
                    return_value=FakeColdQdrantWithPoint(),
                ),
                mock.patch.object(server_memory, "embed_query", return_value=query_vec),
                mock.patch.object(server_memory, "_update_cold_heat"),
            ):
                results = server_memory.search_cold_layer("q", limit=1)

        self.assertIsInstance(results[0]["score"], float)
        self.assertAlmostEqual(results[0]["score"], 0.8)


class FakeHotMemory:
    def __init__(self, results):
        self.results = results
        self.threshold = None

    def search(self, _query, **kwargs):
        self.threshold = kwargs.get("threshold")
        return self.results


class RecallHandlerTest(unittest.IsolatedAsyncioTestCase):
    async def _call(self, hot_results, *, related=None):
        hot = FakeHotMemory(hot_results)
        with (
            mock.patch.object(server_memory, "ensure_hot_memory", new=mock.AsyncMock()),
            mock.patch.object(server_memory, "get_hot_memory", return_value=hot),
            mock.patch.object(server_memory, "search_cold_layer", return_value=[]),
            mock.patch.object(server_memory, "embed_query", return_value=None),
            mock.patch.object(
                server_memory,
                "_expand_hot_episodes",
                return_value=related or [],
            ),
            mock.patch.object(server_memory, "_expand_cold_episodes", return_value=[]),
        ):
            payload = await server_memory._handle_recall({"query": "q", "limit": 5})
        return json.loads(payload[0].text), hot

    async def test_hot_threshold_filters_low_scores_and_marks_direct(self):
        data, hot = await self._call(
            [
                {"id": "low", "memory": "low", "score": 0.5, "metadata": {}},
                {"id": "mid", "memory": "mid", "score": 0.66, "metadata": {}},
                {"id": "high", "memory": "high", "score": 0.8, "metadata": {}},
            ]
        )

        self.assertEqual([row["id"] for row in data["results"]], ["high"])
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["results"][0]["match_kind"], "direct")
        self.assertEqual(hot.threshold, server_memory.RECALL_HOT_MIN_SCORE)

    async def test_only_related_context_is_weak_match(self):
        data, _ = await self._call(
            [{"id": "low", "memory": "low", "score": 0.5, "metadata": {}}],
            related=[
                {
                    "id": "rel",
                    "layer": "hot",
                    "content": "related",
                    "score": 0.6,
                    "expanded": True,
                    "match_kind": "expanded",
                }
            ],
        )

        self.assertEqual(data["status"], "weak_match")
        self.assertEqual(data["count"], 0)
        self.assertEqual(data["related_count"], 1)
        self.assertEqual(data["related"][0]["match_kind"], "expanded")

    async def test_empty_result_is_no_match(self):
        data, _ = await self._call([])

        self.assertEqual(data["status"], "no_match")
        self.assertEqual(data["count"], 0)
        self.assertEqual(data["related_count"], 0)
        self.assertIn("relevance floor", data["hint"])


if __name__ == "__main__":
    unittest.main()
