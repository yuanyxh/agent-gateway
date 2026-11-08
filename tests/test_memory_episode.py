import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QDRANT_PATH", tempfile.mkdtemp(prefix="agent-gateway-test-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_gateway.services.mcp import server_memory, server_state  # noqa: E402
from agent_gateway.services.mcp.server_constants import (  # noqa: E402
    EPISODE_EXPAND_PER_GROUP,
)
from agent_gateway.services.retrieval.arms import recall as recall_arms  # noqa: E402
from agent_gateway.services.retrieval.types import RetrievedItem  # noqa: E402

EPISODE_RE = r"^ep_\d{8}_[0-9a-f]{8}$"
#: Pinned expansion floor so the assertions do not read the calibration file.
EXPANDED_FLOOR = 0.56


def _seed(id_: str, score: float, episode_id: str, layer: str = "hot") -> RetrievedItem:
    return RetrievedItem(
        id=id_,
        kind="memory",
        score=score,
        score_kind="dense",
        layer=layer,
        extra={"episode_id": episode_id},
    )


class EpisodeIdTest(unittest.TestCase):
    def test_new_episode_id_format_and_uniqueness(self):
        first = server_memory._new_episode_id()
        second = server_memory._new_episode_id()

        self.assertRegex(first, EPISODE_RE)
        self.assertNotEqual(first, second)

    def test_build_metadata_keeps_explicit_episode(self):
        meta = server_memory._build_metadata(
            {"content": "x", "episode_id": "ep_shared"}, "t"
        )

        self.assertEqual(meta[server_memory.EPISODE_FIELD], "ep_shared")

    def test_build_metadata_defaults_to_empty(self):
        meta = server_memory._build_metadata({"content": "x"}, "t")

        self.assertEqual(meta[server_memory.EPISODE_FIELD], "")

    def test_build_metadata_keeps_grouping_dimensions(self):
        meta = server_memory._build_metadata(
            {"content": "x", "project": "yjzt", "task": "修复导航"}, "t"
        )

        self.assertEqual(meta["project"], "yjzt")
        self.assertEqual(meta["task"], "修复导航")

    def test_build_metadata_keeps_enum_dimensions(self):
        meta = server_memory._build_metadata(
            {
                "content": "修复登录",
                "subsystem": "auth_service",
                "task_type": "bug",
                "ticket": "PROJ-123",
                "interaction": "correction",
                "session_id": "s-1",
                "role": "root_cause",
            },
            "t",
        )

        self.assertEqual(meta["subsystem"], "auth_service")
        self.assertEqual(meta["task_type"], "bug")
        self.assertEqual(meta["ticket"], "PROJ-123")
        self.assertEqual(meta["interaction"], "correction")
        self.assertEqual(meta["session_id"], "s-1")
        self.assertEqual(meta["narrative_role"], "root_cause")
        self.assertEqual(meta["language"], "zh")

    def test_build_metadata_blanks_unknown_dimension_values(self):
        meta = server_memory._build_metadata(
            {
                "content": "x",
                "task_type": "hotfix",
                "role": "rant",
                "subsystem": "bad\x00slug",
                "ticket": "x" * 200,
            },
            "t",
        )

        for key in ("task_type", "narrative_role", "subsystem", "ticket"):
            self.assertEqual(meta[key], "")

    def test_build_metadata_keeps_free_form_dimension_text(self):
        meta = server_memory._build_metadata(
            {
                "content": "x",
                "project": "我的项目",
                "subsystem": "my subsystem",
                "ticket": "PROJ#123",
            },
            "t",
        )

        self.assertEqual(meta["project"], "我的项目")
        self.assertEqual(meta["subsystem"], "my subsystem")
        self.assertEqual(meta["ticket"], "PROJ#123")

    def test_build_metadata_prefers_explicit_language(self):
        meta = server_memory._build_metadata(
            {"content": "修复登录", "language": "en"}, "t"
        )

        self.assertEqual(meta["language"], "en")

    def test_build_metadata_blanks_unknown_explicit_language(self):
        meta = server_memory._build_metadata(
            {"content": "修复登录", "language": "fr"}, "t"
        )

        self.assertEqual(meta["language"], "")

    def test_build_metadata_detects_english(self):
        meta = server_memory._build_metadata({"content": "fix the parser bug"}, "t")

        self.assertEqual(meta["language"], "en")

    def test_inherit_prefers_superseded_target(self):
        hot = mock.Mock()
        hot.get.return_value = {"metadata": {"episode_id": "ep_from_target"}}
        similar = [{"score": 0.99, "episode_id": "ep_from_similar"}]

        with mock.patch.object(server_memory, "get_hot_memory", return_value=hot):
            result = server_memory._inherit_episode_id(["target"], similar)

        self.assertEqual(result, "ep_from_target")

    def test_inherit_from_similar_above_threshold(self):
        similar = [{"score": 0.85, "episode_id": "ep_similar"}]

        with mock.patch.object(server_memory, "get_hot_memory", return_value=None):
            result = server_memory._inherit_episode_id([], similar)

        self.assertEqual(result, "ep_similar")

    def test_inherit_below_threshold_creates_new(self):
        similar = [{"score": 0.5, "episode_id": "ep_similar"}]

        with mock.patch.object(server_memory, "get_hot_memory", return_value=None):
            result = server_memory._inherit_episode_id([], similar)

        self.assertRegex(result, EPISODE_RE)

    def test_inherit_ignores_empty_episode_on_similar(self):
        similar = [{"score": 0.99, "episode_id": ""}]

        with mock.patch.object(server_memory, "get_hot_memory", return_value=None):
            result = server_memory._inherit_episode_id([], similar)

        self.assertRegex(result, EPISODE_RE)


class FakeVectorStore:
    collection_name = "mem0"

    def __init__(self, points):
        self.client = mock.Mock()
        self.client.scroll.return_value = (points, None)


class FakeHotMemory:
    def __init__(self, points):
        self.vector_store = FakeVectorStore(points)


def _point(point_id, vector=None, **payload):
    point = mock.Mock()
    point.id = point_id
    point.payload = payload
    point.vector = vector
    return point


class BatchEpisodeTest(unittest.TestCase):
    def test_batch_episode_applies_only_to_items_without_own(self):
        written = []

        def fake_write(item, now_iso, db_path=None):
            written.append(item["episode_id"])
            return {"success": True, "memory_id": "m1", "deduped": True}

        async def ready():
            return True

        async def call():
            return await server_memory._handle_remember_batch(
                {
                    "episode_id": "ep_batch",
                    "memories": [
                        {"content": "a"},
                        {"content": "b", "episode_id": "ep_own"},
                    ],
                }
            )

        with (
            mock.patch.object(server_memory, "ensure_hot_memory", ready),
            mock.patch.object(server_memory, "_write_memory", fake_write),
        ):
            asyncio.run(call())

        self.assertEqual(written, ["ep_batch", "ep_own"])

    def test_batch_dimensions_apply_only_to_items_without_own(self):
        written = []

        def fake_write(item, now_iso, db_path=None):
            written.append((item.get("project"), item.get("task")))
            return {"success": True, "memory_id": "m1", "deduped": True}

        async def ready():
            return True

        async def call():
            return await server_memory._handle_remember_batch(
                {
                    "project": "yjzt",
                    "task": "修复导航",
                    "memories": [
                        {"content": "a"},
                        {"content": "b", "project": "utility"},
                    ],
                }
            )

        with (
            mock.patch.object(server_memory, "ensure_hot_memory", ready),
            mock.patch.object(server_memory, "_write_memory", fake_write),
        ):
            asyncio.run(call())

        self.assertEqual(written, [("yjzt", "修复导航"), ("utility", "修复导航")])

    def test_batch_applies_enum_dimensions_to_items_without_own(self):
        written = []

        def fake_write(item, now_iso, db_path=None):
            written.append(
                (item.get("task_type"), item.get("role"), item.get("session_id"))
            )
            return {"success": True, "memory_id": "m1", "deduped": True}

        async def ready():
            return True

        async def call():
            return await server_memory._handle_remember_batch(
                {
                    "task_type": "bug",
                    "role": "root_cause",
                    "session_id": "s-9",
                    "memories": [
                        {"content": "a"},
                        {"content": "b", "role": "decision"},
                    ],
                }
            )

        with (
            mock.patch.object(server_memory, "ensure_hot_memory", ready),
            mock.patch.object(server_memory, "_write_memory", fake_write),
        ):
            asyncio.run(call())

        self.assertEqual(
            written, [("bug", "root_cause", "s-9"), ("bug", "decision", "s-9")]
        )


class EpisodeExpansionTest(unittest.TestCase):
    def test_expands_same_group_once(self):
        seed = _seed("a", 0.9, "ep_1")
        points = [
            _point("a", data="seed", status="active"),
            _point("b", data="sibling", status="active"),
        ]

        with mock.patch.object(
            server_state, "get_hot_memory", return_value=FakeHotMemory(points)
        ):
            expanded = recall_arms._expand_hot([seed], None, EXPANDED_FLOOR)

        self.assertEqual([item.id for item in expanded], ["b"])
        self.assertAlmostEqual(expanded[0].score, 0.9 * 0.98)
        self.assertTrue(expanded[0].extra["expanded"])
        self.assertEqual(expanded[0].extra["episode_id"], "ep_1")
        self.assertEqual(expanded[0].content, "sibling")

    def test_no_episode_means_no_expansion(self):
        points = [_point("b", data="x", status="active")]

        with mock.patch.object(
            server_state, "get_hot_memory", return_value=FakeHotMemory(points)
        ):
            expanded = recall_arms._expand_hot(
                [_seed("a", 0.9, "")], None, EXPANDED_FLOOR
            )

        self.assertEqual(expanded, [])

    def test_skips_archived_and_inactive(self):
        seed = _seed("a", 1.0, "ep_1")
        points = [
            _point("b", data="archived", status="active", archived=True),
            _point("c", data="old", status="superseded"),
        ]

        with mock.patch.object(
            server_state, "get_hot_memory", return_value=FakeHotMemory(points)
        ):
            expanded = recall_arms._expand_hot([seed], None, EXPANDED_FLOOR)

        self.assertEqual(expanded, [])

    def test_caps_rows_per_group(self):
        seed = _seed("a", 1.0, "ep_1")
        points = [_point(f"p{i}", data=str(i), status="active") for i in range(6)]

        with mock.patch.object(
            server_state, "get_hot_memory", return_value=FakeHotMemory(points)
        ):
            expanded = recall_arms._expand_hot([seed], None, EXPANDED_FLOOR)

        self.assertEqual(len(expanded), EPISODE_EXPAND_PER_GROUP)

    def test_ranks_expanded_rows_by_query_similarity(self):
        seed = _seed("a", 0.9, "ep_1")
        points = [
            _point("far", vector=[6.0, 8.0], data="far", status="active"),
            _point("near", vector=[0.99, 0.1], data="near", status="active"),
        ]

        with mock.patch.object(
            server_state, "get_hot_memory", return_value=FakeHotMemory(points)
        ):
            expanded = recall_arms._expand_hot([seed], [1.0, 0.0], EXPANDED_FLOOR)

        self.assertEqual([item.id for item in expanded], ["near", "far"])
        self.assertAlmostEqual(expanded[0].score, 0.9 * 0.98)
        self.assertAlmostEqual(expanded[1].score, 0.6)
        self.assertEqual(expanded[1].match_kind, "expanded")

    def test_drops_sibling_below_relevance_floor(self):
        seed = _seed("a", 0.9, "ep_1")
        points = [
            _point("far", vector=[5.0, 9.0], data="far", status="active"),
            _point("near", vector=[0.99, 0.1], data="near", status="active"),
        ]

        with mock.patch.object(
            server_state, "get_hot_memory", return_value=FakeHotMemory(points)
        ):
            expanded = recall_arms._expand_hot([seed], [1.0, 0.0], EXPANDED_FLOOR)

        self.assertEqual([item.id for item in expanded], ["near"])

    def test_full_group_opens_new_episode(self):
        similar = [{"score": 0.99, "episode_id": "ep_full"}]

        with (
            mock.patch.object(server_state, "get_hot_memory", return_value=None),
            mock.patch.object(
                server_memory,
                "_episode_size",
                return_value=server_memory.EPISODE_MAX_GROUP_SIZE,
            ),
        ):
            result = server_memory._inherit_episode_id([], similar)

        self.assertRegex(result, EPISODE_RE)

    def test_group_below_cap_is_inherited(self):
        similar = [{"score": 0.99, "episode_id": "ep_open"}]

        with (
            mock.patch.object(server_state, "get_hot_memory", return_value=None),
            mock.patch.object(
                server_memory,
                "_episode_size",
                return_value=server_memory.EPISODE_MAX_GROUP_SIZE - 1,
            ),
        ):
            result = server_memory._inherit_episode_id([], similar)

        self.assertEqual(result, "ep_open")


class ColdEpisodeTest(unittest.TestCase):
    def test_explicit_episode_wins(self):
        self.assertEqual(
            server_memory._cold_episode_id(["a"], "ep_explicit"), "ep_explicit"
        )

    def test_no_sources_creates_new(self):
        with mock.patch.object(server_memory, "get_hot_memory", return_value=None):
            result = server_memory._cold_episode_id([])

        self.assertRegex(result, EPISODE_RE)

    def test_inherits_closest_source_episode(self):
        hot = mock.Mock()
        hot.vector_store.client.retrieve.return_value = [
            _point("1", vector=[1.0, 0.0], episode_id="ep_a"),
            _point("2", vector=[0.0, 1.0], episode_id="ep_b"),
        ]

        with mock.patch.object(server_memory, "get_hot_memory", return_value=hot):
            result = server_memory._cold_episode_id(["1", "2"], content_vec=[0.1, 0.9])

        self.assertEqual(result, "ep_b")

    def test_weak_match_opens_new_episode(self):
        hot = mock.Mock()
        hot.vector_store.client.retrieve.return_value = [
            _point("1", vector=[1.0, 0.0], episode_id="ep_a"),
        ]

        with mock.patch.object(server_memory, "get_hot_memory", return_value=hot):
            result = server_memory._cold_episode_id(["1"], content_vec=[0.1, 0.99])

        self.assertRegex(result, EPISODE_RE)

    def test_majority_is_fallback_without_vector(self):
        hot = mock.Mock()
        hot.vector_store.client.retrieve.return_value = [
            _point("1", episode_id="ep_a"),
            _point("2", episode_id="ep_a"),
            _point("3", episode_id="ep_b"),
        ]

        with mock.patch.object(server_memory, "get_hot_memory", return_value=hot):
            result = server_memory._cold_episode_id(["1", "2", "3"])

        self.assertEqual(result, "ep_a")

    def test_lookup_failure_creates_new(self):
        hot = mock.Mock()
        hot.vector_store.client.retrieve.side_effect = RuntimeError("boom")

        with mock.patch.object(server_memory, "get_hot_memory", return_value=hot):
            result = server_memory._cold_episode_id(["1"])

        self.assertRegex(result, EPISODE_RE)


class FakeColdQdrant:
    def __init__(self, points):
        self.points = points

    def scroll(self, **kwargs):
        return (self.points, None)


class ColdEpisodeExpansionTest(unittest.TestCase):
    def test_expands_same_group(self):
        seed = _seed("c1", 0.8, "ep_c", layer="cold")
        points = [_point("c1", content="seed"), _point("c2", content="sibling")]

        with mock.patch.object(
            server_state, "get_qdrant", return_value=FakeColdQdrant(points)
        ):
            expanded = recall_arms._expand_cold([seed], None, EXPANDED_FLOOR)

        self.assertEqual([item.id for item in expanded], ["c2"])
        self.assertEqual(expanded[0].layer, "cold")
        self.assertAlmostEqual(expanded[0].score, 0.8 * 0.98)
        self.assertEqual(expanded[0].extra["episode_id"], "ep_c")

    def test_caps_expanded_score_at_actual_similarity(self):
        seed = _seed("c1", 0.8, "ep_c", layer="cold")
        points = [
            _point("c1", vector=[1.0, 0.0], content="seed"),
            _point("c2", vector=[6.0, 8.0], content="sibling"),
        ]

        with mock.patch.object(
            server_state, "get_qdrant", return_value=FakeColdQdrant(points)
        ):
            expanded = recall_arms._expand_cold([seed], [1.0, 0.0], EXPANDED_FLOOR)

        self.assertEqual([item.id for item in expanded], ["c2"])
        self.assertAlmostEqual(expanded[0].score, 0.6)
        self.assertEqual(expanded[0].match_kind, "expanded")

    def test_skips_stale_and_archived(self):
        seed = _seed("c1", 1.0, "ep_c", layer="cold")
        points = [
            _point("c2", content="x", stale=True),
            _point("c3", content="y", archived=True),
        ]

        with mock.patch.object(
            server_state, "get_qdrant", return_value=FakeColdQdrant(points)
        ):
            expanded = recall_arms._expand_cold([seed], None, EXPANDED_FLOOR)

        self.assertEqual(expanded, [])

    def test_non_cold_seed_ignored(self):
        seed = _seed("h1", 1.0, "ep_h", layer="hot")

        with mock.patch.object(
            server_state, "get_qdrant", return_value=FakeColdQdrant([])
        ):
            expanded = recall_arms._expand_cold([seed], None, EXPANDED_FLOOR)

        self.assertEqual(expanded, [])


class RecallDimensionTest(unittest.TestCase):
    def test_splits_filters_from_boosts(self):
        filters, boost = server_memory._recall_dimensions(
            {
                "project": " p ",
                "ticket": "",
                "language": "zh",
                "task": "t",
                "role": "fact",
                "session_id": "s",
            }
        )

        self.assertEqual(filters, {"project": "p", "language": "zh"})
        self.assertEqual(
            boost, {"task": "t", "narrative_role": "fact", "session_id": "s"}
        )


if __name__ == "__main__":
    unittest.main()
