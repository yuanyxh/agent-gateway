"""Tests for the daily/weekly consolidation stages."""

import asyncio
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

os.environ.setdefault("QDRANT_PATH", tempfile.mkdtemp(prefix="agent-gateway-test-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_gateway.services.memory import consolidate, graph, store  # noqa: E402

NOW = datetime(2026, 11, 1, 3, 0)
NODE_IDS = ("n1", "n2", "n3", "n4")


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self._tmp.name, "graph.sqlite")
        for node_id in NODE_IDS:
            store.upsert_node(
                node_id,
                layer="hot",
                status="active",
                metadata={"episode_id": ""},
                db_path=self.db,
            )

    def tearDown(self):
        self._tmp.cleanup()


class ComponentsTest(unittest.TestCase):
    def test_pairs_form_connected_groups_within_the_cap(self):
        pairs = [("a", "b", 0.9), ("b", "c", 0.9), ("d", "e", 0.9)]

        groups = consolidate._components(pairs, 5)

        self.assertEqual(
            {frozenset(group) for group in groups}, {frozenset("abc"), frozenset("de")}
        )

    def test_groups_above_the_cap_are_dropped(self):
        pairs = [(str(i), str(i + 1), 0.9) for i in range(5)]

        self.assertEqual(consolidate._components(pairs, 3), [])


class BandPairsTest(unittest.TestCase):
    def test_only_pairs_inside_the_band_survive(self):
        rows = {
            "a": {"vector": [1.0, 0.0]},
            "b": {"vector": [0.7, 0.7]},
            "c": {"vector": [0.0, 1.0]},
        }

        pairs = consolidate._band_pairs(["a", "b", "c"], rows, 0.5, 0.75, 10)

        self.assertEqual(len(pairs), 2)
        self.assertEqual(
            {(left, right) for _, left, right in pairs}, {("a", "b"), ("b", "c")}
        )

    def test_limit_keeps_the_closest_pairs(self):
        rows = {
            "a": {"vector": [1.0, 0.0]},
            "b": {"vector": [0.7, 0.7]},
            "c": {"vector": [0.6, 0.8]},
        }

        pairs = consolidate._band_pairs(["a", "b", "c"], rows, 0.5, 0.75, 1)

        self.assertEqual(len(pairs), 1)


class SampleRelationsTest(_Base):
    def _rows(self, same_vector=True):
        return {
            node_id: {
                "vector": [1.0, 0.0] if same_vector else [float(i), 1.0],
                "payload": {"data": node_id},
            }
            for i, node_id in enumerate(NODE_IDS)
        }

    def test_same_day_rerun_walks_the_same_pairs(self):
        with (
            mock.patch.object(consolidate, "_hot_rows", return_value=self._rows()),
            mock.patch.object(consolidate, "_similarity_floor", return_value=0.9),
            mock.patch.object(consolidate.graph, "add_edges", return_value=0),
        ):
            first = consolidate.sample_relations(db_path=self.db, now=NOW)
            second = consolidate.sample_relations(db_path=self.db, now=NOW)

        self.assertEqual(
            {pair[:2] for pair in first["pairs"]},
            {pair[:2] for pair in second["pairs"]},
        )

    def test_generated_summaries_are_excluded_from_sampling(self):
        rows = {
            "n1": {
                "vector": [1.0, 0.0],
                "payload": {"data": "a", "generated_by": "consolidation"},
            },
            "n2": {"vector": [1.0, 0.0], "payload": {"data": "b"}},
            "n3": {"vector": [1.0, 0.0], "payload": {"data": "c"}},
        }
        with (
            mock.patch.object(consolidate, "_hot_rows", return_value=rows),
            mock.patch.object(
                consolidate,
                "active_hot",
                return_value=[{"node_id": key} for key in rows],
            ),
            mock.patch.object(consolidate, "_similarity_floor", return_value=0.9),
            mock.patch.object(consolidate.graph, "add_edges", return_value=0),
        ):
            result = consolidate.sample_relations(db_path=self.db, now=NOW)

        sampled = {node for pair in result["pairs"] for node in pair[:2]}
        self.assertNotIn("n1", sampled)

    def test_pairs_below_the_floor_are_not_related(self):
        rows = {
            "n1": {"vector": [1.0, 0.0], "payload": {"data": "a"}},
            "n2": {"vector": [0.0, 1.0], "payload": {"data": "b"}},
        }
        with (
            mock.patch.object(consolidate, "_hot_rows", return_value=rows),
            mock.patch.object(consolidate, "_similarity_floor", return_value=0.9),
            mock.patch.object(
                consolidate,
                "active_hot",
                return_value=[{"node_id": "n1"}, {"node_id": "n2"}],
            ),
            mock.patch.object(consolidate.graph, "add_edges", return_value=0),
        ):
            result = consolidate.sample_relations(db_path=self.db, now=NOW)

        self.assertEqual(result["pairs"], [])


class CompressTest(_Base):
    def _rows(self):
        return {
            "n1": {"vector": [], "payload": {"data": "fact one"}},
            "n2": {"vector": [], "payload": {"data": "fact two"}},
        }

    def test_summary_is_written_with_derived_from_edges(self):
        with (
            mock.patch.object(
                consolidate.llm_client, "is_configured", return_value=True
            ),
            mock.patch.object(
                consolidate.llm_client,
                "achat",
                mock.AsyncMock(return_value='{"summary": "merged"}'),
            ),
            mock.patch.object(
                consolidate.llm_client,
                "parse_json",
                return_value={"summary": "merged", "context": "why"},
            ),
            mock.patch.object(consolidate.prompt_loader, "prompt", return_value="p"),
            mock.patch(
                "agent_gateway.services.mcp.server_memory._write_memory",
                return_value={"memory_id": "new"},
            ) as write,
            mock.patch.object(consolidate.graph, "add_edges") as add_edges,
        ):
            written = asyncio.run(
                consolidate.compress_clusters(
                    [["n1", "n2"]], self._rows(), db_path=self.db
                )
            )

        self.assertEqual(written, 1)
        item = write.call_args[0][0]
        self.assertEqual(item["generated_by"], "consolidation")
        self.assertEqual(item["content"], "merged")
        edges = add_edges.call_args[0][0]
        self.assertEqual(
            {(edge[0], edge[1], edge[2]) for edge in edges},
            {("new", "n1", "derived_from"), ("new", "n2", "derived_from")},
        )

    def test_clusters_containing_generated_summaries_are_skipped(self):
        rows = self._rows()
        rows["n1"]["payload"]["generated_by"] = "consolidation"
        with (
            mock.patch.object(
                consolidate.llm_client, "is_configured", return_value=True
            ),
            mock.patch.object(
                consolidate.llm_client, "achat", mock.AsyncMock()
            ) as chat,
            mock.patch.object(consolidate.prompt_loader, "prompt", return_value="p"),
        ):
            written = asyncio.run(
                consolidate.compress_clusters([["n1", "n2"]], rows, db_path=self.db)
            )

        self.assertEqual(written, 0)
        chat.assert_not_called()

    def test_fully_covered_cluster_is_skipped(self):
        with (
            mock.patch.object(consolidate, "_uncovered_members", return_value=[]),
            mock.patch.object(
                consolidate.llm_client, "is_configured", return_value=True
            ),
            mock.patch.object(
                consolidate.llm_client, "achat", mock.AsyncMock()
            ) as chat,
            mock.patch.object(consolidate.prompt_loader, "prompt", return_value="p"),
        ):
            written = asyncio.run(
                consolidate.compress_clusters(
                    [["n1", "n2"]], self._rows(), db_path=self.db
                )
            )

        self.assertEqual(written, 0)
        chat.assert_not_called()

    def test_partially_covered_cluster_keeps_only_new_members(self):
        rows = {
            "n1": {"vector": [], "payload": {"data": "fact one"}},
            "n2": {"vector": [], "payload": {"data": "fact two"}},
            "n3": {"vector": [], "payload": {"data": "fact three"}},
        }
        with (
            mock.patch.object(
                consolidate, "_uncovered_members", return_value=["n2", "n3"]
            ),
            mock.patch.object(
                consolidate.llm_client, "is_configured", return_value=True
            ),
            mock.patch.object(
                consolidate.llm_client,
                "achat",
                mock.AsyncMock(return_value='{"summary": "merged"}'),
            ),
            mock.patch.object(
                consolidate.llm_client,
                "parse_json",
                return_value={"summary": "merged"},
            ),
            mock.patch.object(consolidate.prompt_loader, "prompt", return_value="p"),
            mock.patch(
                "agent_gateway.services.mcp.server_memory._write_memory",
                return_value={"memory_id": "new"},
            ),
            mock.patch.object(consolidate.graph, "add_edges") as add_edges,
        ):
            written = asyncio.run(
                consolidate.compress_clusters(
                    [["n1", "n2", "n3"]], rows, db_path=self.db
                )
            )

        self.assertEqual(written, 1)
        edges = add_edges.call_args[0][0]
        self.assertEqual(
            {(edge[0], edge[1]) for edge in edges},
            {("new", "n2"), ("new", "n3")},
        )

    def test_no_llm_means_no_compression(self):
        with mock.patch.object(
            consolidate.llm_client, "is_configured", return_value=False
        ):
            written = asyncio.run(
                consolidate.compress_clusters(
                    [["n1", "n2"]], self._rows(), db_path=self.db
                )
            )

        self.assertEqual(written, 0)


class DailyTest(_Base):
    def test_daily_runs_the_stages_in_order(self):
        sampled = {
            "pairs": [("n1", "n2", 0.8)],
            "edges": 1,
            "clusters": [["n1", "n2"]],
            "rows": {},
        }
        with (
            mock.patch.object(consolidate, "score_relevance", return_value=4) as score,
            mock.patch.object(consolidate, "sample_relations", return_value=sampled),
            mock.patch.object(
                consolidate, "compress_clusters", mock.AsyncMock(return_value=1)
            ) as compress,
        ):
            result = asyncio.run(consolidate.run_daily(db_path=self.db, now=NOW))

        score.assert_called_once()
        compress.assert_awaited_once()
        self.assertEqual(
            result,
            {"scored": 4, "pairs": 1, "edges": 1, "clusters": 1, "compressed": 1},
        )


class NearbyPairsTest(unittest.TestCase):
    def test_neighbour_hits_are_band_filtered_and_deduplicated(self):
        rows = {
            "a": {"vector": [1.0, 0.0]},
            "b": {"vector": [1.0, 0.0]},
            "c": {"vector": [0.0, 1.0]},
        }
        hot = mock.Mock()
        hot.vector_store.collection_name = "mem0"
        hot.vector_store.client.query_batch_points.return_value = [
            mock.Mock(
                points=[mock.Mock(id="b", score=0.6), mock.Mock(id="c", score=0.2)]
            ),
            mock.Mock(points=[mock.Mock(id="a", score=0.6)]),
        ]
        with mock.patch(
            "agent_gateway.services.mcp.server_state.get_hot_memory", return_value=hot
        ):
            pairs = consolidate._nearby_band_pairs(["a", "b", "c"], rows, 0.4, 0.75, 10)

        self.assertEqual(pairs, [(0.6, "a", "b")])

    def test_missing_hot_layer_falls_back(self):
        self.assertIsNone(
            consolidate._nearby_band_pairs(
                ["a", "b"], {"a": {}, "b": {}}, 0.4, 0.75, 10
            )
        )


class ProtectedTest(unittest.TestCase):
    def test_protected_flag_wins(self):
        self.assertTrue(consolidate._is_protected({"protected": True}, None))
        self.assertTrue(consolidate._is_protected({"metadata": {"protected": True}}, 9))

    def test_importance_threshold_is_optional(self):
        self.assertFalse(consolidate._is_protected({"importance": 10}, None))
        self.assertTrue(consolidate._is_protected({"importance": 10}, 8))
        self.assertFalse(consolidate._is_protected({"importance": 5}, 8))


class DryRunTest(unittest.TestCase):
    def test_dry_run_reports_without_writing(self):
        with tempfile.TemporaryDirectory() as root:
            db = os.path.join(root, "graph.sqlite")
            rows = {
                "n1": {"vector": [1.0, 0.0], "payload": {"data": "a"}},
                "n2": {"vector": [1.0, 0.0], "payload": {"data": "b"}},
            }
            with (
                mock.patch.object(
                    consolidate.llm_client, "is_configured", return_value=True
                ),
                mock.patch.object(
                    consolidate,
                    "active_hot",
                    return_value=[{"node_id": "n1"}, {"node_id": "n2"}],
                ),
                mock.patch.object(consolidate, "_hot_rows", return_value=rows),
                mock.patch.object(
                    consolidate,
                    "_candidate_pairs",
                    mock.AsyncMock(return_value=[(0.6, "n1", "n2")]),
                ),
                mock.patch(
                    "agent_gateway.services.mcp.server_memory._check_contradiction",
                    new=mock.AsyncMock(
                        return_value={"relation": "contradiction", "reason": "x"}
                    ),
                ),
                mock.patch.object(consolidate.graph, "add_edge") as add_edge,
                mock.patch(
                    "agent_gateway.services.mcp.server_memory._supersede_older"
                ) as supersede,
            ):
                result = asyncio.run(
                    consolidate.detect_contradictions(db_path=db, now=NOW, dry_run=True)
                )

        self.assertEqual(result["disputes"], 0)
        self.assertEqual(len(result["review"]), 1)
        add_edge.assert_not_called()
        supersede.assert_not_called()


class UncoveredMembersTest(_Base):
    def test_member_linked_to_a_summary_is_dropped(self):
        store.upsert_node(
            "sum",
            layer="hot",
            status="active",
            metadata={"generated_by": "consolidation"},
            db_path=self.db,
        )
        graph.add_edge("sum", "n1", "derived_from", db_path=self.db)

        self.assertEqual(consolidate._uncovered_members(["n1", "n2"], self.db), ["n2"])

    def test_plain_memories_are_all_uncovered(self):
        self.assertEqual(
            consolidate._uncovered_members(["n1", "n2"], self.db), ["n1", "n2"]
        )


class ContradictionSkipTest(_Base):
    def test_pairs_with_a_dispute_edge_are_not_judged_again(self):
        rows = {
            "n1": {"vector": [1.0, 0.0], "payload": {"data": "fact one"}},
            "n2": {"vector": [0.7, 0.7], "payload": {"data": "fact two"}},
        }
        graph.add_edge("n1", "n2", "disputes", db_path=self.db)
        with (
            mock.patch.object(
                consolidate.llm_client, "is_configured", return_value=True
            ),
            mock.patch.object(
                consolidate,
                "active_hot",
                return_value=[{"node_id": "n1"}, {"node_id": "n2"}],
            ),
            mock.patch.object(consolidate, "_hot_rows", return_value=rows),
            mock.patch(
                "agent_gateway.services.mcp.server_memory._check_contradiction",
                new=mock.AsyncMock(),
            ) as judge,
        ):
            result = asyncio.run(
                consolidate.detect_contradictions(db_path=self.db, now=NOW)
            )

        judge.assert_not_awaited()
        self.assertEqual(result, {"pairs": 1, "disputes": 0})


class WeeklyTest(_Base):
    def test_weekly_combines_disputes_and_forgetting(self):
        with (
            mock.patch.object(
                consolidate,
                "detect_contradictions",
                mock.AsyncMock(return_value={"pairs": 2, "disputes": 1}),
            ),
            mock.patch.object(consolidate, "forget_expired", return_value=3),
        ):
            result = asyncio.run(consolidate.run_weekly(db_path=self.db, now=NOW))

        self.assertEqual(
            result, {"disputes": {"pairs": 2, "disputes": 1}, "archived": 3}
        )


if __name__ == "__main__":
    unittest.main()
