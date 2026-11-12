"""Tests for entity extraction, parsing and storage."""

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

from agent_gateway.services.memory import entities, store  # noqa: E402

PAYLOAD = {
    "memories": [
        {
            "id": "m1",
            "entities": [
                {"name": "绣仪", "type": "person", "aliases": ["Xiu Yi"]},
                {"name": "折耳根", "type": "food", "aliases": []},
            ],
            "facts": [
                {
                    "subject": "绣仪",
                    "predicate": "不吃",
                    "object": "折耳根",
                    "polarity": "negative",
                    "confidence": 0.9,
                }
            ],
        },
        {"id": "m2", "entities": [], "facts": []},
    ]
}


class ParseTests(unittest.TestCase):
    def test_invalid_json_returns_empty(self):
        self.assertEqual(entities.parse_batch("not json"), {})

    def test_payload_maps_by_memory_id(self):
        parsed = entities.parse_batch(json.dumps(PAYLOAD, ensure_ascii=False))
        self.assertEqual(set(parsed), {"m1", "m2"})
        self.assertEqual(len(parsed["m1"]["entities"]), 2)
        self.assertEqual(parsed["m1"]["facts"][0]["predicate"], "不吃")

    def test_entries_without_name_are_dropped(self):
        raw = json.dumps(
            {
                "memories": [
                    {"id": "m1", "entities": [{"type": "person"}, {"name": " A "}]}
                ]
            }
        )
        parsed = entities.parse_batch(raw)
        self.assertEqual([item["name"] for item in parsed["m1"]["entities"]], [" A "])

    def test_entity_id_is_case_insensitive(self):
        self.assertEqual(entities.entity_id("Alice"), entities.entity_id(" alice "))


class StorageTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self._tmp.name, "graph.sqlite")

    def tearDown(self):
        self._tmp.cleanup()

    def test_upsert_is_idempotent(self):
        parsed = entities.parse_batch(json.dumps(PAYLOAD, ensure_ascii=False))
        self.assertEqual(entities.store_batch(parsed, db_path=self.db), 2)
        self.assertEqual(entities.store_batch(parsed, db_path=self.db), 2)
        with store.connect(self.db) as connection:
            names = connection.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
            mentions = connection.execute(
                "SELECT COUNT(*) FROM entity_mentions"
            ).fetchone()[0]
        self.assertEqual(names, 2)
        self.assertEqual(mentions, 2)

    def test_upsert_keeps_aliases_when_new_list_is_empty(self):
        parsed = {"m1": {"entities": [{"name": "Alice", "aliases": ["Al"]}]}}
        entities.store_batch(parsed, db_path=self.db)
        entities.store_batch(
            {"m1": {"entities": [{"name": "Alice", "aliases": []}]}}, db_path=self.db
        )
        with store.connect(self.db) as connection:
            row = connection.execute(
                "SELECT aliases FROM entities WHERE name='Alice'"
            ).fetchone()
        self.assertEqual(json.loads(row[0]), ["Al"])

    def test_extract_batch_calls_model_once_for_ten(self):
        memories = [{"id": f"m{index}", "text": f"fact {index}"} for index in range(10)]
        raw = json.dumps({"memories": [{"id": item["id"]} for item in memories]})
        with (
            mock.patch.object(entities.llm_client, "is_configured", return_value=True),
            mock.patch.object(
                entities.llm_client, "achat", new=mock.AsyncMock(return_value=raw)
            ) as chat,
        ):
            parsed = asyncio.run(entities.extract_batch(memories))
        self.assertEqual(chat.await_count, 1)
        self.assertEqual(len(parsed), 10)

    def test_extract_batch_drops_unknown_ids(self):
        memories = [{"id": "m1", "text": "fact"}]
        raw = json.dumps({"memories": [{"id": "m1"}, {"id": "ghost"}]})
        with (
            mock.patch.object(entities.llm_client, "is_configured", return_value=True),
            mock.patch.object(
                entities.llm_client, "achat", new=mock.AsyncMock(return_value=raw)
            ),
        ):
            parsed = asyncio.run(entities.extract_batch(memories))
        self.assertEqual(set(parsed), {"m1"})

    def test_extract_without_model_returns_empty(self):
        with mock.patch.object(
            entities.llm_client, "is_configured", return_value=False
        ):
            parsed = asyncio.run(entities.extract_batch([{"id": "m1", "text": "x"}]))
        self.assertEqual(parsed, {})


class FactTimelineTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self._tmp.name, "graph.sqlite")
        self.now = "2026-06-01T00:00:00"

    def tearDown(self):
        self._tmp.cleanup()

    def fact(self, obj):
        return {"subject": "绣仪", "predicate": "不吃", "object": obj}

    def rows(self):
        with store.connect(self.db) as connection:
            return connection.execute(
                "SELECT to_id, valid_to, supports, contradicts FROM memory_edges"
            ).fetchall()

    def test_same_fact_only_adds_support(self):
        with store.connect(self.db) as connection:
            self.assertEqual(
                entities.merge_fact(
                    connection, self.fact("折耳根"), "m1", now=self.now
                ),
                "inserted",
            )
        with store.connect(self.db) as connection:
            self.assertEqual(
                entities.merge_fact(
                    connection, self.fact("折耳根"), "m2", now=self.now
                ),
                "updated",
            )
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][2], 2)
        self.assertIsNone(rows[0][1])

    def test_conflicting_object_closes_previous(self):
        with store.connect(self.db) as connection:
            entities.merge_fact(connection, self.fact("折耳根"), "m1", now=self.now)
        later = "2026-07-01T00:00:00"
        with store.connect(self.db) as connection:
            entities.merge_fact(connection, self.fact("香菜"), "m2", now=later)
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        closed = [row for row in rows if row[1] is not None]
        active = [row for row in rows if row[1] is None]
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0][3], 1)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0][1] is None, True)

    def test_current_facts_returns_only_active(self):
        with store.connect(self.db) as connection:
            entities.merge_fact(connection, self.fact("折耳根"), "m1", now=self.now)
            entities.merge_fact(
                connection, self.fact("香菜"), "m2", now="2026-07-01T00:00:00"
            )
            facts = entities.current_facts(connection, "绣仪")
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["predicate"], "不吃")

    def test_consolidate_promotes_supported_only(self):
        with store.connect(self.db) as connection:
            entities.merge_fact(connection, self.fact("折耳根"), "m1", now=self.now)
            entities.merge_fact(connection, self.fact("折耳根"), "m2", now=self.now)
            entities.merge_fact(
                connection,
                {"subject": "绣仪", "predicate": "喜欢", "object": "香菜"},
                "m3",
                now=self.now,
            )
            promoted = entities.consolidate_facts(connection, min_support=2)
            weights = dict(
                (row[0], row[1])
                for row in connection.execute(
                    "SELECT type, weight FROM memory_edges"
                ).fetchall()
            )
        self.assertEqual(promoted, 1)
        self.assertEqual(weights["不吃"], 1.0)

    def test_same_evidence_does_not_inflate_supports(self):
        with store.connect(self.db) as connection:
            entities.merge_fact(connection, self.fact("折耳根"), "m1", now=self.now)
            action = entities.merge_fact(
                connection, self.fact("折耳根"), "m1", now=self.now
            )
        self.assertEqual(action, "duplicate")
        rows = self.rows()
        self.assertEqual(rows[0][2], 1)

    def test_fact_metadata_keeps_polarity_and_confidence(self):
        fact = {**self.fact("折耳根"), "polarity": "negative", "confidence": 0.9}
        with store.connect(self.db) as connection:
            entities.merge_fact(connection, fact, "m1", now=self.now)
            meta = json.loads(
                connection.execute("SELECT metadata FROM memory_edges").fetchone()[0]
            )
        self.assertEqual(meta["polarity"], "negative")
        self.assertEqual(meta["confidence"], 0.9)
        self.assertEqual(meta["evidence"], ["m1"])

    def test_store_facts_is_idempotent_per_evidence(self):
        parsed = {
            "m1": {"entities": [], "facts": [self.fact("折耳根")]},
        }
        entities.store_facts(parsed, db_path=self.db, now=self.now)
        counts = entities.store_facts(parsed, db_path=self.db, now=self.now)
        self.assertEqual(counts.get("duplicate"), 1)
        self.assertEqual(self.rows()[0][2], 1)

    def test_skips_incomplete_facts(self):
        with store.connect(self.db) as connection:
            action = entities.merge_fact(
                connection, {"subject": "绣仪", "predicate": ""}, "m1", now=self.now
            )
        self.assertEqual(action, "skipped")


if __name__ == "__main__":
    unittest.main()
