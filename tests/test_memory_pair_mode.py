"""Tests for v2/v3 pair building: anchors, sparse overlap and cold nodes."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QDRANT_PATH", tempfile.mkdtemp(prefix="agent-gateway-test-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import numpy as np  # noqa: E402

from agent_gateway.services.memory import network_build, sparse, store  # noqa: E402


class _Point:
    def __init__(self, point_id, score):
        self.id = point_id
        self.score = score


class _Result:
    def __init__(self, points):
        self.points = points


class _Hot:
    """Returns fixed neighbors per anchor, in the order anchors were queried."""

    def __init__(self, anchors, neighbors):
        self.vector_store = self
        self.collection_name = "test"
        self.client = self
        self.anchors = anchors
        self.neighbors = neighbors
        self.offset = 0

    def query_batch_points(self, collection_name, requests):
        results = []
        for index in range(len(requests)):
            anchor = self.anchors[self.offset + index]
            results.append(
                _Result([_Point(pid, score) for pid, score in self.neighbors[anchor]])
            )
        self.offset += len(requests)
        return results


def _groups(count, members=1):
    return {
        f"n{index}": [{"node_id": f"m{index}", "metadata": {"fact": f"fact {index}"}}]
        for index in range(count)
    }


def _centroids(count, dims=768):
    rng = np.random.default_rng(11)
    return {
        f"n{index}": rng.standard_normal(dims).astype(np.float32)
        for index in range(count)
    }


class _FlakyHot:
    """Batch queries fail (mixed dims), single-anchor queries still work."""

    class _Store:
        collection_name = "test"

    def __init__(self):
        self.vector_store = self._Store()
        self.vector_store.client = self

    def query_batch_points(self, collection_name, requests):
        if len(requests) > 1:
            raise RuntimeError("dimension mismatch")
        return [_Result([_Point("m0", 0.9), _Point("m1", 0.9)])]


class PairModeTests(unittest.TestCase):
    def setUp(self):
        self.groups = _groups(2)
        self.centroids = _centroids(2)
        self.hot = _Hot(
            ["n0", "n1"],
            {"n0": [("m1", 0.9)], "n1": [("m0", 0.9)]},
        )

    def test_v2_skips_single_member_groups(self):
        result = network_build.candidate_pairs(
            self.groups, self.centroids, hot=self.hot, pair_mode="v2"
        )
        self.assertEqual(result["pairs"], [])

    def test_v3_keeps_single_member_groups(self):
        result = network_build.candidate_pairs(
            self.groups,
            self.centroids,
            hot=_Hot(["n0", "n1"], {"n0": [("m1", 0.9)], "n1": [("m0", 0.9)]}),
            pair_mode="v3",
        )
        self.assertEqual(len(result["pairs"]), 1)
        self.assertEqual(result["pairs"][0]["left_group"], "n0")

    def test_v3_combines_cosine_and_overlap(self):
        rng = np.random.default_rng(5)
        vector = rng.standard_normal(768)
        codes = {
            "n0": sparse.decode(sparse.encode(vector)),
            "n1": sparse.decode(
                sparse.encode(vector + 0.05 * rng.standard_normal(768))
            ),
        }
        result = network_build.candidate_pairs(
            self.groups,
            self.centroids,
            hot=_Hot(["n0", "n1"], {"n0": [("m1", 0.9)], "n1": [("m0", 0.9)]}),
            pair_mode="v3",
            codes=codes,
        )
        score = result["pairs"][0]["score"]
        self.assertGreater(score, 0.5)
        self.assertLess(score, 0.95)

    def test_mixed_dim_batch_falls_back_to_single_queries(self):
        result = network_build.candidate_pairs(
            self.groups, self.centroids, hot=_FlakyHot(), pair_mode="v3"
        )
        self.assertEqual(len(result["pairs"]), 1)

    def test_group_centroids_falls_back_to_cold_vectors(self):
        groups = {"n0": [{"node_id": "m0"}]}
        loader = lambda ids: {"m0": [0.5, 0.5, 0.5, 0.5]}  # noqa: E731
        centroids, missing = network_build.group_centroids(
            groups, None, cold_loader=loader
        )
        self.assertEqual(missing, 0)
        self.assertAlmostEqual(float(np.linalg.norm(centroids["n0"])), 1.0)

    def test_group_centroids_merges_hot_and_cold(self):
        class _Point:
            id = "m0"
            vector = [1.0, 0.0]

        class _Store:
            collection_name = "hot"

            def retrieve(self, **kwargs):
                return [_Point()]

        class _Hot:
            vector_store = _Store()

        groups = {"n0": [{"node_id": "m0"}, {"node_id": "cold:m1"}]}
        loader = lambda ids: {"cold:m1": [0.0, 1.0]}  # noqa: E731
        centroids, missing = network_build.group_centroids(
            groups, _Hot(), cold_loader=loader
        )
        self.assertEqual(missing, 0)
        self.assertAlmostEqual(float(np.linalg.norm(centroids["n0"])), 1.0)

    def test_group_centroids_cold_loader_error_is_degraded(self):
        groups = {"n0": [{"node_id": "m0"}]}

        def loader(ids):
            raise RuntimeError("cold store down")

        centroids, missing = network_build.group_centroids(
            groups, None, cold_loader=loader
        )
        self.assertEqual(centroids, {})
        self.assertEqual(missing, 1)

    def test_centroid_fallback_pairs_without_vector_store(self):
        rng = np.random.default_rng(3)
        vector = rng.standard_normal(768).astype(np.float32)
        centroids = {
            "n0": vector,
            "n1": vector + 0.01 * rng.standard_normal(768).astype(np.float32),
        }
        result = network_build.candidate_pairs(
            self.groups, centroids, hot=None, pair_mode="v3"
        )
        self.assertEqual(len(result["pairs"]), 1)
        self.assertGreater(result["pairs"][0]["score"], 0.8)

    def test_centroid_fallback_skips_mismatched_dims(self):
        rng = np.random.default_rng(7)
        base = rng.standard_normal(8).astype(np.float32)
        groups = _groups(3)
        centroids = {
            "n0": base,
            "n1": base + 0.01 * rng.standard_normal(8).astype(np.float32),
            "n2": base[:4].copy(),
        }
        result = network_build.candidate_pairs(
            groups, centroids, hot=None, pair_mode="v3"
        )
        pairs = {(pair["left_group"], pair["right_group"]) for pair in result["pairs"]}
        self.assertEqual(pairs, {("n0", "n1")})

    def test_stored_centroids_read_from_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "graph.sqlite")
            now = "2026-06-01T00:00:00"
            with store.connect(db) as connection:
                connection.execute(
                    "INSERT INTO neurons (neuron_id, member_count, tier, status, "
                    "centroid, created_at, updated_at) VALUES "
                    "('n0', 1, 'active', 'active', ?, ?, ?)",
                    (np.ones(4, dtype=np.float32).tobytes(), now, now),
                )
            centroids, missing = network_build._stored_centroids(_groups(2), db)
            self.assertEqual(missing, 1)
            self.assertAlmostEqual(float(np.linalg.norm(centroids["n0"])), 1.0)

    def test_v3_sparse_floor_drops_unrelated_pairs(self):
        codes = {
            "n0": sparse.decode(sparse.encode(np.ones(768))),
            "n1": sparse.decode(
                sparse.encode(np.random.default_rng(9).standard_normal(768))
            ),
        }
        result = network_build.candidate_pairs(
            self.groups,
            self.centroids,
            hot=_Hot(["n0", "n1"], {"n0": [("m1", 0.9)], "n1": [("m0", 0.9)]}),
            pair_mode="v3",
            codes=codes,
            sparse_floor=0.5,
        )
        self.assertEqual(result["pairs"], [])


class ActiveNodeTests(unittest.TestCase):
    def test_cold_layer_nodes_are_included(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "graph.sqlite")
            now = "2026-06-01T00:00:00"
            with store.connect(db) as connection:
                connection.execute(
                    "INSERT INTO memory_nodes (node_id, layer, status, content_hash, "
                    "title, created_at, updated_at) VALUES ('cold1', 'cold', 'active', "
                    "'h', 'cold', ?, ?)",
                    (now, now),
                )
                connection.execute(
                    "INSERT INTO memory_nodes (node_id, layer, status, content_hash, "
                    "title, created_at, updated_at) VALUES ('hot1', 'hot', 'active', "
                    "'h2', 'hot', ?, ?)",
                    (now, now),
                )
            nodes = network_build._active_nodes(db)
            self.assertEqual({node["node_id"] for node in nodes}, {"cold1", "hot1"})


if __name__ == "__main__":
    unittest.main()
