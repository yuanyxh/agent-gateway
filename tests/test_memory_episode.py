import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QDRANT_PATH", tempfile.mkdtemp(prefix="agent-gateway-test-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_gateway.services.mcp import server_memory  # noqa: E402

EPISODE_RE = r"^ep_\d{8}_[0-9a-f]{8}$"


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


class EpisodeExpansionTest(unittest.TestCase):
    def test_expands_same_group_once(self):
        seed = {"id": "a", "score": 0.9, "episode_id": "ep_1", "layer": "hot"}
        points = [
            _point("a", data="seed", status="active"),
            _point("b", data="sibling", status="active"),
        ]

        with mock.patch.object(
            server_memory, "get_hot_memory", return_value=FakeHotMemory(points)
        ):
            expanded = server_memory._expand_hot_episodes([seed])

        self.assertEqual([row["id"] for row in expanded], ["b"])
        self.assertAlmostEqual(expanded[0]["score"], 0.9 * 0.98)
        self.assertTrue(expanded[0]["expanded"])
        self.assertEqual(expanded[0]["episode_id"], "ep_1")
        self.assertEqual(expanded[0]["content"], "sibling")

    def test_no_episode_means_no_expansion(self):
        points = [_point("b", data="x", status="active")]

        with mock.patch.object(
            server_memory, "get_hot_memory", return_value=FakeHotMemory(points)
        ):
            expanded = server_memory._expand_hot_episodes([{"id": "a", "score": 0.9}])

        self.assertEqual(expanded, [])

    def test_skips_archived_and_inactive(self):
        seed = {"id": "a", "score": 1.0, "episode_id": "ep_1", "layer": "hot"}
        points = [
            _point("b", data="archived", status="active", archived=True),
            _point("c", data="old", status="superseded"),
        ]

        with mock.patch.object(
            server_memory, "get_hot_memory", return_value=FakeHotMemory(points)
        ):
            expanded = server_memory._expand_hot_episodes([seed])

        self.assertEqual(expanded, [])

    def test_caps_rows_per_group(self):
        seed = {"id": "a", "score": 1.0, "episode_id": "ep_1", "layer": "hot"}
        points = [_point(f"p{i}", data=str(i), status="active") for i in range(6)]

        with mock.patch.object(
            server_memory, "get_hot_memory", return_value=FakeHotMemory(points)
        ):
            expanded = server_memory._expand_hot_episodes([seed])

        self.assertEqual(len(expanded), server_memory.EPISODE_EXPAND_PER_GROUP)

    def test_ranks_expanded_rows_by_query_similarity(self):
        seed = {"id": "a", "score": 0.9, "episode_id": "ep_1", "layer": "hot"}
        points = [
            _point("far", vector=[6.0, 8.0], data="far", status="active"),
            _point("near", vector=[0.99, 0.1], data="near", status="active"),
        ]

        with mock.patch.object(
            server_memory, "get_hot_memory", return_value=FakeHotMemory(points)
        ):
            expanded = server_memory._expand_hot_episodes([seed], query_vec=[1.0, 0.0])

        self.assertEqual([row["id"] for row in expanded], ["near", "far"])
        self.assertAlmostEqual(expanded[0]["score"], 0.9 * 0.98)
        self.assertAlmostEqual(expanded[1]["score"], 0.6)
        self.assertEqual(expanded[1]["match_kind"], "expanded")

    def test_drops_sibling_below_relevance_floor(self):
        seed = {"id": "a", "score": 0.9, "episode_id": "ep_1", "layer": "hot"}
        points = [
            _point("far", vector=[5.0, 9.0], data="far", status="active"),
            _point("near", vector=[0.99, 0.1], data="near", status="active"),
        ]

        with mock.patch.object(
            server_memory, "get_hot_memory", return_value=FakeHotMemory(points)
        ):
            expanded = server_memory._expand_hot_episodes([seed], query_vec=[1.0, 0.0])

        self.assertEqual([row["id"] for row in expanded], ["near"])

    def test_full_group_opens_new_episode(self):
        similar = [{"score": 0.99, "episode_id": "ep_full"}]

        with (
            mock.patch.object(server_memory, "get_hot_memory", return_value=None),
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
            mock.patch.object(server_memory, "get_hot_memory", return_value=None),
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
        seed = {"id": "c1", "score": 0.8, "episode_id": "ep_c", "layer": "cold"}
        points = [_point("c1", content="seed"), _point("c2", content="sibling")]

        with mock.patch.object(
            server_memory, "get_qdrant", return_value=FakeColdQdrant(points)
        ):
            expanded = server_memory._expand_cold_episodes([seed])

        self.assertEqual([row["id"] for row in expanded], ["c2"])
        self.assertEqual(expanded[0]["layer"], "cold")
        self.assertAlmostEqual(expanded[0]["score"], 0.8 * 0.98)
        self.assertEqual(expanded[0]["episode_id"], "ep_c")

    def test_caps_expanded_score_at_actual_similarity(self):
        seed = {"id": "c1", "score": 0.8, "episode_id": "ep_c", "layer": "cold"}
        points = [
            _point("c1", vector=[1.0, 0.0], content="seed"),
            _point("c2", vector=[6.0, 8.0], content="sibling"),
        ]

        with mock.patch.object(
            server_memory, "get_qdrant", return_value=FakeColdQdrant(points)
        ):
            expanded = server_memory._expand_cold_episodes([seed], query_vec=[1.0, 0.0])

        self.assertEqual([row["id"] for row in expanded], ["c2"])
        self.assertAlmostEqual(expanded[0]["score"], 0.6)
        self.assertEqual(expanded[0]["match_kind"], "expanded")

    def test_skips_stale_and_archived(self):
        seed = {"id": "c1", "score": 1.0, "episode_id": "ep_c", "layer": "cold"}
        points = [
            _point("c2", content="x", stale=True),
            _point("c3", content="y", archived=True),
        ]

        with mock.patch.object(
            server_memory, "get_qdrant", return_value=FakeColdQdrant(points)
        ):
            expanded = server_memory._expand_cold_episodes([seed])

        self.assertEqual(expanded, [])

    def test_non_cold_seed_ignored(self):
        seed = {"id": "h1", "score": 1.0, "episode_id": "ep_h", "layer": "hot"}

        with mock.patch.object(
            server_memory, "get_qdrant", return_value=FakeColdQdrant([])
        ):
            expanded = server_memory._expand_cold_episodes([seed])

        self.assertEqual(expanded, [])


if __name__ == "__main__":
    unittest.main()
