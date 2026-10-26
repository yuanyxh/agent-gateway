import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_gateway.core.db import conn as dbconn
from agent_gateway.services.knowledge import knowledge_pool as pool


class KnowledgePoolRotationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(
            os.environ,
            {"UTILITY_DATA_DIR": self.tmp.name},
            clear=False,
        )
        self.env.start()
        pool.init_db()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def _card(self, title: str) -> int:
        return pool.insert_card(kind=pool.KIND_TOPIC, title=title, body="body")

    def test_unseen_cards_are_served_before_reviewed(self):
        self._card("first")
        self._card("second")
        self.assertEqual(pool.unseen_count(pool.KIND_TOPIC), 2)

        first = pool.next_card(pool.KIND_TOPIC)
        second = pool.next_card(pool.KIND_TOPIC)
        self.assertEqual([first["title"], second["title"]], ["first", "second"])
        self.assertEqual(pool.unseen_count(pool.KIND_TOPIC), 0)
        self.assertIsNone(pool.next_card(pool.KIND_TOPIC))

    def test_reviewed_cards_are_opt_in(self):
        self._card("first")
        pool.next_card(pool.KIND_TOPIC)
        self.assertIsNone(pool.next_card(pool.KIND_TOPIC))

        reviewed = pool.next_card(pool.KIND_TOPIC, show_reviewed=True, max_shown=2)
        self.assertEqual(reviewed["title"], "first")
        self.assertIsNone(
            pool.next_card(pool.KIND_TOPIC, show_reviewed=True, max_shown=2)
        )

    def test_seed_only_loads_topics(self):
        topics = '[{"name":"TCP/IP","kind":"principle"}]'
        with patch.dict(os.environ, {"KNOWLEDGE_TOPICS": topics}, clear=False):
            self.assertEqual(pool.seed_from_config(), 1)
            self.assertEqual(pool.seed_from_config(), 0)

    def test_seed_resyncs_scope_of_existing_topic(self):
        old = '[{"name":"TCP/IP","kind":"principle","scope":"old","exclude":"old"}]'
        new = '[{"name":"TCP/IP","kind":"principle","scope":"new","exclude":"new"}]'
        with patch.dict(os.environ, {"KNOWLEDGE_TOPICS": old}, clear=False):
            pool.seed_from_config()
        with patch.dict(os.environ, {"KNOWLEDGE_TOPICS": new}, clear=False):
            self.assertEqual(pool.seed_from_config(), 0)
        topic = pool.next_topic()
        self.assertEqual((topic["scope"], topic["exclude"]), ("new", "new"))

    def test_prune_expired_keeps_liked_and_recent(self):
        old_id = self._card("old")
        liked_id = self._card("old-liked")
        self._card("new")
        with dbconn(pool.db_path()) as conn:
            conn.execute(
                "UPDATE cards SET created_at = '2024-01-01T00:00:00+00:00' WHERE id = ?",
                (old_id,),
            )
            conn.execute(
                "UPDATE cards SET created_at = '2024-01-01T00:00:00+00:00',"
                " status = 'liked' WHERE id = ?",
                (liked_id,),
            )
        self.assertEqual(pool.prune_expired(365), 1)
        with dbconn(pool.db_path()) as conn:
            titles = [r[0] for r in conn.execute("SELECT title FROM cards ORDER BY id")]
        self.assertEqual(titles, ["old-liked", "new"])
        self.assertEqual(pool.prune_expired(0), 0)

    def test_disliked_titles_feed_newest_first(self):
        topics = '[{"name":"TCP/IP","kind":"principle"}]'
        with patch.dict(os.environ, {"KNOWLEDGE_TOPICS": topics}, clear=False):
            pool.seed_from_config()
        topic_id = pool.next_topic()["id"]
        for title in ("first", "second"):
            card_id = pool.insert_card(
                kind=pool.KIND_TOPIC, title=title, body="body", topic_id=topic_id
            )
            pool.mark_status(card_id, "disliked")
        self.assertEqual(pool.disliked_titles(topic_id), ["second", "first"])


if __name__ == "__main__":
    unittest.main()
