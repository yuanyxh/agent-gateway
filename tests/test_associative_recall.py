"""Tests for associative recall: spreading, scoring, reinforcement and output."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

os.environ.setdefault("QDRANT_PATH", tempfile.mkdtemp(prefix="agent-gateway-test-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_gateway.core import config  # noqa: E402
from agent_gateway.services.memory import (  # noqa: E402
    network,
    network_build,
    store,
    strength,
)
from agent_gateway.services.retrieval.arms import associative  # noqa: E402
from agent_gateway.services.retrieval.types import RetrievedItem  # noqa: E402

VECTORS = {
    "a1": [1.0, 0.0],
    "b1": [0.95, 0.05],
    "b2": [0.9, 0.1],
    "c1": [0.95, 0.05],
    "c2": [0.9, 0.1],
    "d1": [0.0, 1.0],
}


class _Point:
    def __init__(self, point_id, vector, payload):
        self.id = point_id
        self.vector = vector
        self.payload = payload


class _Client:
    def retrieve(self, collection_name, ids, with_payload, with_vectors):
        return [
            _Point(point_id, VECTORS[point_id], {"data": point_id})
            for point_id in ids
            if point_id in VECTORS
        ]


class _VectorStore:
    def __init__(self):
        self.client = _Client()
        self.collection_name = "mem0"


class _Hot:
    def __init__(self):
        self.vector_store = _VectorStore()


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self._tmp.name, "graph.sqlite")
        for node_id, episode in (
            ("a1", "ep_a"),
            ("b1", "ep_b"),
            ("b2", "ep_b"),
            ("c1", "ep_c"),
            ("c2", "ep_c"),
            ("d1", "ep_d"),
        ):
            store.upsert_node(
                node_id,
                layer="hot",
                status="active",
                title=node_id,
                metadata={"episode_id": episode, "fact": node_id},
                db_path=self.db,
            )
        network_build.migrate_network(db_path=self.db, apply=True)
        network.upsert_synapse(
            {"from_neuron": "ep_a", "to_neuron": "ep_b", "weight": 0.9},
            db_path=self.db,
        )
        network.upsert_synapse(
            {"from_neuron": "ep_b", "to_neuron": "ep_c", "weight": 0.9},
            db_path=self.db,
        )
        network.upsert_synapse(
            {"from_neuron": "ep_a", "to_neuron": "ep_d", "weight": 0.2},
            db_path=self.db,
        )
        self.hot = _Hot()

    def tearDown(self):
        self._tmp.cleanup()


class SpreadTest(_Base):
    def test_strong_chain_reaches_and_weak_chain_fades(self):
        result = associative.spread(
            [
                {
                    "memory_id": "a1",
                    "neuron_id": "ep_a",
                    "activation": 0.6,
                }
            ],
            db_path=self.db,
            depth=3,
            beam=5,
            floor=0.15,
        )
        by_id = {item["neuron_id"]: item for item in result["activated"]}
        self.assertGreater(by_id["ep_b"]["activation"], by_id["ep_c"]["activation"])
        self.assertNotIn("ep_d", by_id)
        self.assertEqual(len(by_id["ep_c"]["path"]), 3)
        self.assertFalse(result["truncated"])

    def test_stronger_second_path_wins_over_first_reach(self):
        network.upsert_synapse(
            {"from_neuron": "x", "to_neuron": "z", "weight": 0.3},
            db_path=self.db,
        )
        network.upsert_synapse(
            {"from_neuron": "x", "to_neuron": "y", "weight": 1.0},
            db_path=self.db,
        )
        network.upsert_synapse(
            {"from_neuron": "y", "to_neuron": "z", "weight": 1.0},
            db_path=self.db,
        )
        result = associative.spread(
            [{"neuron_id": "x", "activation": 1.0}],
            db_path=self.db,
            depth=2,
            beam=5,
            floor=0.0,
        )
        by_id = {item["neuron_id"]: item["activation"] for item in result["activated"]}
        hop = associative.HOP_DECAY * associative.NEIGHBOR_HEAT_FACTOR
        self.assertAlmostEqual(by_id["z"], hop**2, places=4)

    def test_visited_cap_truncates(self):
        result = associative.spread(
            [{"neuron_id": "ep_a", "activation": 1.0}],
            db_path=self.db,
            depth=3,
            beam=5,
            floor=0.0,
            visited_cap=2,
        )
        self.assertTrue(result["truncated"])
        self.assertLessEqual(result["visited"], 2)


class ScoreTest(_Base):
    def test_tier_penalty_and_archive_filter(self):
        network.upsert_activation(
            "b1", {"tier": "warm", "strength": 0.3}, db_path=self.db
        )
        network.upsert_activation(
            "c1", {"tier": "archive", "strength": 0.1}, db_path=self.db
        )
        activated = [
            {"neuron_id": "ep_b", "activation": 0.4, "path": [{"neuron": "ep_a"}]},
            {"neuron_id": "ep_c", "activation": 0.4, "path": [{"neuron": "ep_a"}]},
        ]
        items = associative.score_members(
            activated,
            np.asarray([1.0, 0.0]),
            min_score=0.1,
            hot=self.hot,
            db_path=self.db,
        )
        by_id = {item.id: item for item in items}
        self.assertIn("b1", by_id)
        self.assertNotIn("c1", by_id)
        warm = by_id["b1"]
        self.assertGreater(warm.score, 0.1)
        self.assertEqual(warm.match_kind, "associated")
        self.assertEqual(warm.extra["neuron_id"], "ep_b")
        self.assertEqual(warm.extra["hops"], 0)

        included = associative.score_members(
            activated,
            np.asarray([1.0, 0.0]),
            min_score=0.1,
            hot=self.hot,
            include_archived=True,
            db_path=self.db,
        )
        self.assertIn("c1", {item.id for item in included})

    def test_exclude_drops_already_returned_members(self):
        activated = [
            {"neuron_id": "ep_a", "activation": 0.6, "path": [{"neuron": "ep_a"}]},
        ]
        items = associative.score_members(
            activated,
            np.asarray([1.0, 0.0]),
            min_score=0.1,
            hot=self.hot,
            exclude={"a1"},
            db_path=self.db,
        )
        self.assertNotIn("a1", {item.id for item in items})


class ReinforceTest(_Base):
    def test_reinforce_after_recall_updates_memory_and_path(self):
        network.upsert_activation(
            "a1", {"strength": 0.2, "tier": "active"}, db_path=self.db
        )
        link = network.synapses_of("ep_a", db_path=self.db)[0]
        before = link["weight"]
        items = [
            {
                "id": "a1",
                "path": [
                    {"memory": "a1", "neuron": "ep_a"},
                    {
                        "synapse_id": link["synapse_id"],
                        "from": "ep_a",
                        "to": "ep_b",
                        "weight": before,
                    },
                ],
            }
        ]
        result = strength.reinforce_after_recall(items, db_path=self.db)
        self.assertEqual(result, {"memories": 1, "synapses": 1})
        self.assertGreater(
            network.get_activation("a1", db_path=self.db)["strength"], 0.2
        )
        after = network.synapses_of("ep_a", db_path=self.db)[0]["weight"]
        self.assertGreater(after, before)

    def test_disabled_skips_reinforcement(self):
        before = network.get_activation("a1", db_path=self.db)
        with mock.patch.object(config, "memory_neuron_enabled", return_value=False):
            result = strength.reinforce_after_recall([{"id": "a1"}], db_path=self.db)
        self.assertEqual(result, {"memories": 0, "synapses": 0})
        self.assertEqual(network.get_activation("a1", db_path=self.db), before)


class OutputTest(unittest.TestCase):
    def test_result_exposes_path_and_hops(self):
        item = RetrievedItem(
            id="m1",
            kind="memory",
            score=0.8,
            score_kind="dense",
            content="x",
            layer="hot",
            match_kind="associated",
            extra={
                "path": [{"neuron": "ep_a"}, {"neuron": "ep_b"}],
                "hops": 1,
                "neuron_id": "ep_b",
                "activation": 0.4,
            },
        )
        result = item.to_result()
        self.assertEqual(result["neuron_id"], "ep_b")
        self.assertEqual(result["hops"], 1)
        self.assertEqual(len(result["path"]), 2)


class SwitchTest(_Base):
    def test_disabled_returns_empty(self):
        with mock.patch.object(config, "memory_neuron_enabled", return_value=False):
            result = associative.associative_expand(
                mock.Mock(text="query", root_text="query"),
                [],
                {},
                hot=self.hot,
                db_path=self.db,
            )
        self.assertEqual(result, [])


class SoftSeedDbPathTest(_Base):
    def test_soft_seeds_resolve_in_given_db(self):
        default_db = os.path.join(self._tmp.name, "default.sqlite")

        class Hot:
            def search(self, *args, **kwargs):
                return [{"id": "a1", "score": 0.9}]

        with mock.patch.object(store, "graph_db_path", return_value=Path(default_db)):
            seeds = associative.soft_seeds(
                mock.Mock(text="x"), hot=Hot(), exclude=set(), db_path=self.db
            )
        self.assertEqual(seeds[0]["neuron_id"], "ep_a")


if __name__ == "__main__":
    unittest.main()
