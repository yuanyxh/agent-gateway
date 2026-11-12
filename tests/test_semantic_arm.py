"""Tests for the semantic (entity) retrieval arm."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QDRANT_PATH", tempfile.mkdtemp(prefix="agent-gateway-test-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_gateway.core import config  # noqa: E402
from agent_gateway.services.memory import entities, store  # noqa: E402
from agent_gateway.services.retrieval.arms.semantic import SemanticArm  # noqa: E402
from agent_gateway.services.retrieval.types import Query  # noqa: E402


class SemanticArmTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self._tmp.name, "graph.sqlite")
        now = "2026-06-01T00:00:00"
        with store.connect(self.db) as connection:
            connection.execute(
                "INSERT INTO memory_nodes (node_id, layer, status, content_hash, "
                "title, created_at, updated_at) VALUES ('m1', 'hot', 'active', 'h', "
                "'绣仪不吃折耳根', ?, ?)",
                (now, now),
            )
            connection.execute(
                "INSERT INTO memory_nodes (node_id, layer, status, content_hash, "
                "title, created_at, updated_at) VALUES ('m2', 'hot', 'archived', 'h2', "
                "'旧记忆', ?, ?)",
                (now, now),
            )
            entities.upsert_entities(
                connection,
                "m1",
                {"entities": [{"name": "绣仪", "type": "person", "aliases": []}]},
                now=now,
            )
            entities.upsert_entities(
                connection,
                "m2",
                {"entities": [{"name": "绣仪", "type": "person", "aliases": []}]},
                now=now,
            )

    def tearDown(self):
        self._tmp.cleanup()

    def test_entity_mention_hits_memory(self):
        arm = SemanticArm(db_path=self.db)
        result = arm.retrieve(Query(text="绣仪最近怎么样"), 5)
        self.assertEqual([item.id for item in result.items], ["m1"])
        self.assertEqual(result.items[0].match_kind, "semantic_fact")

    def test_archived_memory_is_excluded(self):
        arm = SemanticArm(db_path=self.db)
        result = arm.retrieve(Query(text="绣仪"), 5)
        self.assertNotIn("m2", [item.id for item in result.items])

    def test_unknown_entity_returns_nothing(self):
        arm = SemanticArm(db_path=self.db)
        result = arm.retrieve(Query(text="完全无关的句子"), 5)
        self.assertEqual(result.items, [])

    def test_switch_off_disables_arm(self):
        arm = SemanticArm(db_path=self.db)
        with mock.patch.object(config, "memory_brain_semantic", return_value=False):
            result = arm.retrieve(Query(text="绣仪"), 5)
        self.assertEqual(result.items, [])
        self.assertEqual(result.error, "disabled")

    def test_empty_query_returns_nothing(self):
        arm = SemanticArm(db_path=self.db)
        self.assertEqual(arm.retrieve(Query(text=""), 5).items, [])


if __name__ == "__main__":
    unittest.main()
