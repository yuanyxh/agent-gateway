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

from agent_gateway.services.mcp import (  # noqa: E402
    server_embed,
    server_memory,
    server_state,
)
from agent_gateway.services.retrieval.arms import recall as recall_arms  # noqa: E402

from agent_gateway.services.retrieval.types import Query, RetrievedItem  # noqa: E402

#: Pinned values so the assertions do not depend on the active model's
#: calibration file.
FLOORS = {
    "hot": 0.68,
    "cold": 0.56,
    "expanded": 0.56,
    "notes_coverage": 0.60,
    "notes_semantic": 0.40,
}


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
            with mock.patch.object(recall_arms, "NOTES_DIR", root):
                arm = recall_arms.NotesArm(min_coverage=FLOORS["notes_coverage"])
                result = arm.retrieve(Query(text="RSS 治理 日报 规则 改进"), limit=5)

        self.assertEqual([item.id for item in result.items], ["notes:full.md"])
        self.assertAlmostEqual(result.items[0].score, 0.6)
        self.assertEqual(result.items[0].extra["heat_score"], 1.0)


class FakeColdQdrant:
    def __init__(self, points=None):
        self.kwargs = None
        self.points = points or []

    def query_points(self, **kwargs):
        self.kwargs = kwargs
        return mock.Mock(points=self.points)


class ColdThresholdTest(unittest.TestCase):
    def test_query_points_receives_score_threshold(self):
        qdrant = FakeColdQdrant()
        query_vec = mock.Mock()
        query_vec.tolist.return_value = [1.0, 0.0]
        with (
            mock.patch.object(server_state, "get_qdrant", return_value=qdrant),
            mock.patch.object(server_embed, "embed_query", return_value=query_vec),
        ):
            arm = recall_arms.ColdMemoryArm(min_score=FLOORS["cold"])
            arm.retrieve(Query(text="q"), limit=3)

        self.assertEqual(qdrant.kwargs["score_threshold"], FLOORS["cold"])


class FakeColdPoint:
    id = "cold-1"
    score = np.float32(0.8)
    payload = {"content": "cold", "source_file": "x.md"}


class ColdScoreSerializationTest(unittest.TestCase):
    def test_cold_score_and_cosine_are_python_floats(self):
        self.assertIsInstance(
            recall_arms.cosine(
                np.array([1.0, 0.0], dtype=np.float32),
                np.array([6.0, 8.0], dtype=np.float32),
            ),
            float,
        )
        query_vec = mock.Mock()
        query_vec.tolist.return_value = [1.0, 0.0]
        with (
            mock.patch.object(
                server_state,
                "get_qdrant",
                return_value=FakeColdQdrant([FakeColdPoint()]),
            ),
            mock.patch.object(server_embed, "embed_query", return_value=query_vec),
            mock.patch.object(recall_arms, "update_cold_heat"),
        ):
            arm = recall_arms.ColdMemoryArm(min_score=FLOORS["cold"])
            result = arm.retrieve(Query(text="q"), limit=1)

        self.assertIsInstance(result.items[0].score, float)
        self.assertAlmostEqual(result.items[0].score, 0.8)


class NotesSemanticFilterTest(unittest.TestCase):
    def _items(self):
        return [
            RetrievedItem(
                id="keep",
                kind="note",
                score=0.6,
                score_kind="lexical",
                content="on topic",
            ),
            RetrievedItem(
                id="drop", kind="note", score=0.6, score_kind="lexical", content="noise"
            ),
        ]

    def test_drops_snippet_below_semantic_floor(self):
        arm = recall_arms.NotesArm(min_coverage=0.0, min_semantic=0.4)
        with (
            mock.patch.object(
                recall_arms.server_embed,
                "embed_query",
                return_value=np.array([1.0, 0.0], dtype=np.float32),
            ),
            mock.patch.object(
                recall_arms.server_embed,
                "embed_texts",
                return_value=[
                    np.array([1.0, 0.0], dtype=np.float32),
                    np.array([0.0, 1.0], dtype=np.float32),
                ],
            ),
        ):
            kept = arm._semantic_filter("q", self._items())

        self.assertEqual([item.id for item in kept], ["keep"])

    def test_disabled_floor_keeps_everything(self):
        arm = recall_arms.NotesArm(min_coverage=0.0, min_semantic=0.0)
        items = self._items()
        self.assertEqual(arm._semantic_filter("q", items), items)


class FakeHotMemory:
    def __init__(self, results):
        self.results = results
        self.threshold = None

    def search(self, _query, **kwargs):
        self.threshold = kwargs.get("threshold")
        return self.results


class SignalFieldTest(unittest.TestCase):
    def test_hot_arm_reads_quality_from_the_graph(self):
        hot = FakeHotMemory([{"id": "m1", "memory": "x", "score": 0.9, "metadata": {}}])
        with (
            mock.patch.object(server_state, "get_hot_memory", return_value=hot),
            mock.patch.object(
                recall_arms.memory_store,
                "get_nodes",
                return_value={"m1": {"metadata": {"relevance": 0.8}}},
            ),
        ):
            arm = recall_arms.HotMemoryArm(min_score=0.0)
            result = arm.retrieve(Query(text="q", language="en", root_text="q"), 3)

        self.assertEqual(result.items[0].extra["relevance"], 0.8)


class RelevanceFailureTest(unittest.TestCase):
    def test_graph_failure_keeps_the_hot_results(self):
        hot = FakeHotMemory([{"id": "m1", "memory": "x", "score": 0.9, "metadata": {}}])
        with (
            mock.patch.object(server_state, "get_hot_memory", return_value=hot),
            mock.patch.object(
                recall_arms.memory_store,
                "get_nodes",
                side_effect=RuntimeError("database is locked"),
            ),
        ):
            result = recall_arms.HotMemoryArm(min_score=0.0).retrieve(
                Query(text="q", language="en", root_text="q"), 3
            )

        self.assertEqual([item.id for item in result.items], ["m1"])
        self.assertFalse(result.error)


class TouchAccessTest(unittest.TestCase):
    def test_access_counter_moves_for_hot_rows_only(self):
        hot = mock.Mock()
        hot.get.return_value = {"metadata": {"access_count": 2}}
        with (
            mock.patch.object(server_memory, "get_hot_memory", return_value=hot),
            mock.patch.object(
                server_memory, "_update_metadata", return_value=True
            ) as update,
        ):
            server_memory._touch_hot_access(
                [{"id": "m1", "layer": "hot"}, {"id": "c1", "layer": "cold"}]
            )

        update.assert_called_once()
        self.assertEqual(update.call_args[0][1]["access_count"], 3)


class RecallHandlerTest(unittest.IsolatedAsyncioTestCase):
    async def _call(self, hot_results, *, related=None, query="q"):
        hot = FakeHotMemory(hot_results)
        with tempfile.TemporaryDirectory() as root:
            with (
                mock.patch.object(
                    server_memory, "ensure_hot_memory", new=mock.AsyncMock()
                ),
                mock.patch.object(server_state, "get_hot_memory", return_value=hot),
                mock.patch.object(server_state, "get_qdrant", return_value=None),
                mock.patch.object(server_embed, "embed_query", return_value=None),
                mock.patch.object(recall_arms, "NOTES_DIR", root),
                mock.patch.object(
                    server_memory, "expand_episodes", return_value=related or []
                ),
                mock.patch.object(
                    server_memory, "floors_for", return_value=dict(FLOORS)
                ),
            ):
                payload = await server_memory._handle_recall(
                    {"query": query, "limit": 5}
                )
        return json.loads(payload[0].text), hot

    async def test_top_level_archive_flag_is_honoured(self):
        data, _ = await self._call(
            [
                {
                    "id": "arch",
                    "memory": "x",
                    "score": 0.9,
                    "archived": True,
                    "metadata": {},
                }
            ]
        )

        self.assertEqual(data["results"], [])

    async def test_top_level_status_is_honoured(self):
        data, _ = await self._call(
            [
                {
                    "id": "arch",
                    "memory": "x",
                    "score": 0.9,
                    "status": "archived",
                    "metadata": {},
                }
            ]
        )

        self.assertEqual(data["results"], [])

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
        self.assertNotIn("match_kind", data["results"][0])
        # The floor moved into the arm: mem0 filters on the semantic score before
        # hybrid scoring, which silently drops verbatim hits that need rescuing.
        self.assertEqual(hot.threshold, 0.0)

    async def test_verbatim_hit_survives_the_floor(self):
        data, _ = await self._call(
            [
                {
                    "id": "m",
                    "memory": "张三丰是项目组成员",
                    "score": 0.42,
                    "score_details": {"semantic_score": 0.42},
                    "metadata": {},
                }
            ],
            query="张三丰",
        )

        self.assertEqual([row["id"] for row in data["results"]], ["m"])

    async def test_low_score_without_verbatim_hit_is_dropped(self):
        data, _ = await self._call(
            [
                {
                    "id": "m",
                    "memory": "完全无关的内容",
                    "score": 0.42,
                    "score_details": {"semantic_score": 0.42},
                    "metadata": {},
                }
            ],
            query="张三丰",
        )

        self.assertEqual(data["results"], [])

    async def test_hot_keeps_row_whose_hybrid_score_halved_the_semantic_one(self):
        """mem0 divides by 2 when BM25 is active, so 0.75 cosine shows up as 0.37."""
        data, _ = await self._call(
            [
                {
                    "id": "m",
                    "memory": "m",
                    "score": 0.37,
                    "score_details": {"semantic_score": 0.75},
                    "metadata": {},
                }
            ]
        )

        self.assertEqual([row["id"] for row in data["results"]], ["m"])
        self.assertEqual(data["results"][0]["layer"], "hot")

    async def test_hot_drops_row_when_semantic_score_is_below_floor(self):
        data, _ = await self._call(
            [
                {
                    "id": "m",
                    "memory": "m",
                    "score": 0.9,
                    "score_details": {"semantic_score": 0.5},
                    "metadata": {},
                }
            ]
        )

        self.assertEqual(data["results"], [])

    async def test_only_related_context_is_weak_match(self):
        data, _ = await self._call(
            [{"id": "low", "memory": "low", "score": 0.5, "metadata": {}}],
            related=[
                RetrievedItem(
                    id="rel",
                    kind="memory",
                    score=0.6,
                    score_kind="dense",
                    content="related",
                    layer="hot",
                    match_kind="expanded",
                    extra={"expanded": True},
                )
            ],
        )

        self.assertEqual(data["status"], "weak_match")
        self.assertEqual(data["count"], 0)
        self.assertEqual(len(data["related"]), 1)
        self.assertNotIn("match_kind", data["related"][0])

    async def test_empty_result_is_no_match(self):
        data, _ = await self._call([])

        self.assertEqual(data["status"], "no_match")
        self.assertEqual(data["count"], 0)
        self.assertEqual(data["related"], [])
        self.assertIn("relevance floor", data["hint"])


if __name__ == "__main__":
    unittest.main()
