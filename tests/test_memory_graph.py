"""Tests for the memory graph: node registry, edge traversal, and the tools."""

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

from agent_gateway.services.mcp import server_memory_graph  # noqa: E402
from agent_gateway.services.memory import graph, link, store  # noqa: E402


class _GraphCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self._tmp.name, "graph.sqlite")

    def tearDown(self):
        self._tmp.cleanup()

    def node(self, node_id, layer="hot", status="active", episode="", **metadata):
        store.upsert_node(
            node_id,
            layer=layer,
            status=status,
            metadata={"episode_id": episode, **metadata},
            db_path=self.db,
        )


class StoreTest(_GraphCase):
    def test_cold_node_id_is_deterministic_and_source_scoped(self):
        body = store.content_hash("same body")
        first = store.cold_node_id("a.md", body)
        self.assertEqual(first, store.cold_node_id("a.md", body))
        self.assertNotEqual(first, store.cold_node_id("b.md", body))

    def test_cold_point_id_is_a_stable_uuid(self):
        first = store.cold_point_id("a.md", "body")
        self.assertEqual(first, store.cold_point_id("a.md", "body"))
        self.assertNotEqual(first, store.cold_point_id("a.md", "other"))

    def test_batch_metadata_update_patches_many_rows(self):
        self.node("m1")
        self.node("m2")

        updated = store.batch_update_metadata(
            [("m1", {"relevance": 0.5}), ("m2", {"relevance": 0.9})],
            db_path=self.db,
        )

        self.assertEqual(updated, 2)
        self.assertEqual(
            store.get_node("m1", db_path=self.db)["metadata"]["relevance"], 0.5
        )
        self.assertEqual(
            store.get_node("m2", db_path=self.db)["metadata"]["relevance"], 0.9
        )

    def test_upsert_keeps_the_original_created_at(self):
        store.upsert_node(
            "m1", layer="hot", status="active", created_at="2026-01-01", db_path=self.db
        )
        store.upsert_node(
            "m1",
            layer="hot",
            status="superseded",
            created_at="2026-02-01",
            db_path=self.db,
        )
        node = store.get_node("m1", db_path=self.db)
        self.assertEqual(node["status"], "superseded")
        self.assertEqual(node["created_at"], "2026-01-01")

    def test_find_node_by_point_id(self):
        store.upsert_node(
            "cold:1",
            layer="cold",
            status="active",
            metadata={"point_id": "p1"},
            db_path=self.db,
        )
        self.assertEqual(
            store.find_node_by_point_id("p1", db_path=self.db)["node_id"], "cold:1"
        )

    def test_find_node_by_legacy_point_id(self):
        store.upsert_node(
            "cold:1",
            layer="cold",
            status="active",
            metadata={"point_id": "p2", "legacy_ids": ["p1"]},
            db_path=self.db,
        )
        self.assertEqual(
            store.find_node_by_point_id("p1", db_path=self.db)["node_id"], "cold:1"
        )

    def test_find_node_by_backfill_legacy_id(self):
        store.upsert_node(
            "cold:2",
            layer="cold",
            status="active",
            metadata={"point_id": "p3", "legacy_id": "p2"},
            db_path=self.db,
        )
        self.assertEqual(
            store.find_node_by_point_id("p2", db_path=self.db)["node_id"], "cold:2"
        )


class GraphWalkTest(_GraphCase):
    def test_version_chain_walks_to_both_ends(self):
        for node_id in ("v1", "v2", "v3"):
            self.node(node_id)
        graph.add_edges(
            [("v2", "v1", "supersedes"), ("v3", "v2", "supersedes")], db_path=self.db
        )

        result = graph.walk("v2", "supersedes", db_path=self.db)

        self.assertEqual(
            {item["node_id"] for item in result["nodes"]}, {"v1", "v2", "v3"}
        )
        self.assertEqual(len(result["edges"]), 2)
        self.assertEqual(result["missing"], [])
        self.assertFalse(result["truncated"])

    def test_episode_star_walks_the_whole_group(self):
        for node_id in ("e1", "e2", "e3"):
            self.node(node_id, episode="ep1")
        graph.add_edges(
            [("e1", "e2", "episode"), ("e1", "e3", "episode")], db_path=self.db
        )

        result = graph.walk("e3", "episode", db_path=self.db)

        self.assertEqual(
            {item["node_id"] for item in result["nodes"]}, {"e1", "e2", "e3"}
        )

    def test_source_chain_reaches_sources_and_their_versions(self):
        for node_id in ("h1", "h2", "h3"):
            self.node(node_id)
        self.node("cold:1", layer="cold")
        graph.add_edges(
            [
                ("cold:1", "h2", "derived_from"),
                ("h3", "h2", "supersedes"),
                ("h2", "h1", "supersedes"),
            ],
            db_path=self.db,
        )

        result = graph.walk_sources("cold:1", db_path=self.db)

        self.assertEqual(
            {item["node_id"] for item in result["nodes"]},
            {"cold:1", "h1", "h2", "h3"},
        )

    def test_walk_reports_edges_to_unregistered_nodes(self):
        self.node("v2")
        graph.add_edge("v2", "gone", "supersedes", db_path=self.db)

        result = graph.walk("v2", "supersedes", db_path=self.db)

        self.assertEqual(result["missing"], ["gone"])

    def test_unknown_edge_type_is_rejected(self):
        with self.assertRaises(ValueError):
            graph.add_edge("a", "b", "similar", db_path=self.db)

    def test_has_edge_matches_symmetric_edges_either_way(self):
        graph.add_edge("a", "b", "disputes", db_path=self.db)

        self.assertTrue(graph.has_edge("a", "b", "disputes", db_path=self.db))
        self.assertTrue(graph.has_edge("b", "a", "disputes", db_path=self.db))
        self.assertFalse(graph.has_edge("a", "c", "disputes", db_path=self.db))

    def test_has_edge_keeps_direction_for_version_chains(self):
        graph.add_edge("new", "old", "supersedes", db_path=self.db)

        self.assertTrue(graph.has_edge("new", "old", "supersedes", db_path=self.db))
        self.assertFalse(graph.has_edge("old", "new", "supersedes", db_path=self.db))


class LinkTest(_GraphCase):
    def test_hot_write_records_node_supersede_and_episode(self):
        self.node("m0", episode="ep1")
        link.record_hot_memory(
            "m1",
            "new fact",
            {
                "episode_id": "ep1",
                "revision": 2,
                "visibility": "agent-specific",
                "owner_agent_id": "agent-a",
            },
            supersedes=("m0",),
            created_at="2026-03-01",
            db_path=self.db,
        )

        node = store.get_node("m1", db_path=self.db)
        self.assertEqual(node["layer"], "hot")
        self.assertEqual(node["metadata"]["episode_id"], "ep1")
        self.assertEqual(node["metadata"]["visibility"], "agent-specific")
        self.assertEqual(node["metadata"]["owner_agent_id"], "agent-a")
        edge_types = {
            (edge["from_id"], edge["to_id"], edge["type"])
            for edge in graph.neighbors("m1", db_path=self.db)
        }
        self.assertIn(("m1", "m0", "supersedes"), edge_types)
        self.assertIn(("m1", "m0", "episode"), edge_types)

    def test_cold_write_links_back_to_sources(self):
        self.node("h1")
        node_id = link.record_cold_memory(
            "p1",
            "summary",
            source_file="a.md",
            source_memory_ids=["h1"],
            episode_id="ep1",
            db_path=self.db,
        )

        node = store.get_node(node_id, db_path=self.db)
        self.assertEqual(node["layer"], "cold")
        self.assertEqual(node["source_file"], "a.md")
        edges = graph.neighbors(node_id, db_path=self.db)
        self.assertIn(
            (node_id, "h1", "derived_from"),
            {(edge["from_id"], edge["to_id"], edge["type"]) for edge in edges},
        )

    def test_cold_rewrite_keeps_the_previous_point_id(self):
        first = link.record_cold_memory(
            "p1", "body", source_file="a.md", db_path=self.db
        )
        second = link.record_cold_memory(
            "p2", "body", source_file="a.md", db_path=self.db
        )
        self.assertEqual(first, second)
        metadata = store.get_node(first, db_path=self.db)["metadata"]
        self.assertEqual(metadata["point_id"], "p2")
        self.assertIn("p1", metadata["legacy_ids"])

    def test_status_change_updates_the_node(self):
        self.node("m1")
        link.record_hot_status(
            ["m1"], "superseded", superseded_by="m2", db_path=self.db
        )

        self.assertEqual(store.get_node("m1", db_path=self.db)["status"], "superseded")


class ToolTest(_GraphCase):
    def test_memory_get_returns_the_hot_body(self):
        self.node("m1")
        hot = mock.Mock()
        hot.get.return_value = {"memory": "hello"}
        with (
            mock.patch.object(
                server_memory_graph.store, "graph_db_path", return_value=Path(self.db)
            ),
            mock.patch.object(server_memory_graph, "get_hot_memory", return_value=hot),
        ):
            out = asyncio.run(server_memory_graph.handle_memory_get({"node_id": "m1"}))

        data = json.loads(out[0].text)
        self.assertTrue(data["found"])
        self.assertEqual(data["content"], "hello")

    def test_memory_get_accepts_a_cold_point_id(self):
        store.upsert_node(
            "cold:1",
            layer="cold",
            status="active",
            metadata={"point_id": "p1"},
            db_path=self.db,
        )
        point = mock.Mock()
        point.payload = {"content": "cold body"}
        qdrant = mock.Mock()
        qdrant.retrieve.return_value = [point]
        with (
            mock.patch.object(
                server_memory_graph.store, "graph_db_path", return_value=Path(self.db)
            ),
            mock.patch.object(server_memory_graph, "get_qdrant", return_value=qdrant),
        ):
            out = asyncio.run(server_memory_graph.handle_memory_get({"node_id": "p1"}))

        data = json.loads(out[0].text)
        self.assertTrue(data["found"])
        self.assertEqual(data["content"], "cold body")

    def test_memory_graph_walks_the_version_chain(self):
        for node_id in ("v1", "v2"):
            self.node(node_id)
        graph.add_edge("v2", "v1", "supersedes", db_path=self.db)
        with mock.patch.object(
            server_memory_graph.store, "graph_db_path", return_value=Path(self.db)
        ):
            out = asyncio.run(
                server_memory_graph.handle_memory_graph(
                    {"node_id": "v2", "edge": "supersedes"}
                )
            )

        data = json.loads(out[0].text)
        self.assertTrue(data["found"])
        self.assertEqual({item["node_id"] for item in data["nodes"]}, {"v1", "v2"})

    def test_unknown_node_is_reported_not_raised(self):
        with mock.patch.object(
            server_memory_graph.store, "graph_db_path", return_value=Path(self.db)
        ):
            out = asyncio.run(
                server_memory_graph.handle_memory_get({"node_id": "missing"})
            )

        self.assertFalse(json.loads(out[0].text)["found"])


class SchemaTest(unittest.TestCase):
    def test_tool_enum_matches_edge_types(self):
        from agent_gateway.services.mcp.server_tool_schema import build_tools

        tool = next(item for item in build_tools() if item.name == "memory_graph")
        self.assertEqual(
            tuple(tool.input_schema["properties"]["edge"]["enum"]), graph.EDGE_TYPES
        )


if __name__ == "__main__":
    unittest.main()
