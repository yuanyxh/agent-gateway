"""Tests for the lifecycle archive state, quality retention, and contradiction band."""

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

os.environ.setdefault("QDRANT_PATH", tempfile.mkdtemp(prefix="agent-gateway-test-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_gateway.core import memory_lifecycle as lc  # noqa: E402
from agent_gateway.services.mcp import server_memory  # noqa: E402
from agent_gateway.services.memory import retention  # noqa: E402

NOW = datetime(2026, 11, 1)


def _row(access=0, idle_days=0, **extra):
    return {
        "access_count": access,
        "last_accessed_at": (NOW - timedelta(days=idle_days)).isoformat(),
        **extra,
    }


class ArchiveStatusTest(unittest.TestCase):
    def _run(self, metadata, status):
        hot = mock.Mock()
        hot.get.return_value = {"metadata": metadata}
        written = {}

        def capture(memory_id, meta):
            written.update(meta)
            return True

        with (
            mock.patch.object(server_memory, "get_hot_memory", return_value=hot),
            mock.patch.object(server_memory, "_update_metadata", side_effect=capture),
            mock.patch.object(server_memory, "_mark_cold_stale", return_value=0),
            mock.patch.object(server_memory.memory_link, "record_hot_status"),
        ):
            result = server_memory.set_memory_status(["m1"], status)
        self.assertEqual(result["updated"], 1)
        return written

    def test_archive_sets_the_flag_and_retires_the_entry(self):
        written = self._run(
            {
                lc.STATUS_FIELD: lc.STATUS_ACTIVE,
                lc.PROCESSED_FIELD: lc.PROCESSED_SUMMARIZED_MASK,
            },
            lc.STATUS_ARCHIVED,
        )

        self.assertEqual(written[lc.STATUS_FIELD], lc.STATUS_ARCHIVED)
        self.assertTrue(written["archived"])
        self.assertNotEqual(written[lc.ARCHIVED_AT_FIELD], "")
        self.assertEqual(
            written[lc.PROCESSED_FIELD],
            lc.PROCESSED_SUMMARIZED_MASK | lc.PROCESSED_RETIRED_MASK,
        )

    def test_restore_clears_the_archive_and_keeps_the_summarized_bit(self):
        written = self._run(
            {
                lc.STATUS_FIELD: lc.STATUS_ARCHIVED,
                lc.ARCHIVED_AT_FIELD: "2026-01-01T00:00:00",
                "archived": True,
                lc.PROCESSED_FIELD: lc.PROCESSED_CONSUMED_MASK,
            },
            lc.STATUS_ACTIVE,
        )

        self.assertFalse(written["archived"])
        self.assertEqual(written[lc.ARCHIVED_AT_FIELD], "")
        self.assertEqual(written[lc.PROCESSED_FIELD], lc.PROCESSED_SUMMARIZED_MASK)

    def test_unknown_status_is_rejected(self):
        with self.assertRaises(ValueError):
            server_memory.set_memory_status(["m1"], "lost")


class DisputeStampTest(unittest.TestCase):
    def test_dispute_writes_a_timestamp(self):
        hot = mock.Mock()
        hot.get.return_value = {"memory": "old", "metadata": {}}
        written = {}

        def capture(memory_id, meta):
            written.update(meta)
            return True

        with (
            mock.patch.object(server_memory, "get_hot_memory", return_value=hot),
            mock.patch.object(server_memory, "_update_metadata", side_effect=capture),
            mock.patch.object(server_memory.memory_link, "record_dispute"),
        ):
            ok = server_memory._mark_hot_disputed("m1", "m2", "conflict")

        self.assertTrue(ok)
        self.assertTrue(written["disputed"])
        self.assertNotEqual(written[lc.DISPUTED_AT_FIELD], "")


class HotArchiveWriteTest(unittest.TestCase):
    def test_protected_point_is_not_archived(self):
        from agent_gateway.services.mcp import server_archive

        point = mock.Mock()
        point.id = "p1"
        point.payload = {"metadata": {"protected": True}}
        vector_store = mock.Mock()
        vector_store.collection_name = "mem0"
        vector_store._create_filter.return_value = None
        vector_store.client.scroll.return_value = ([point], None)
        hot = mock.Mock()
        hot.vector_store = vector_store

        with (
            mock.patch.object(
                server_archive.retention, "is_expired", return_value=True
            ),
            mock.patch.object(server_archive.memory_link, "record_hot_status"),
        ):
            archived, _, _ = server_archive.run_archive_sync_impl(hot, None, NOW)

        self.assertEqual(archived, 0)
        vector_store.client.set_payload.assert_not_called()

    def test_archive_writes_state_into_metadata(self):
        from agent_gateway.services.mcp import server_archive

        point = mock.Mock()
        point.id = "m1"
        point.payload = {"metadata": {"status": "active", "tags": ["t"], "fact": "f"}}
        vector_store = mock.Mock()
        vector_store.collection_name = "mem0"
        vector_store._create_filter.return_value = None
        vector_store.client.scroll.return_value = ([point], None)
        hot = mock.Mock()
        hot.vector_store = vector_store

        with (
            mock.patch.object(
                server_archive.retention, "is_expired", return_value=True
            ),
            mock.patch.object(server_archive.memory_link, "record_hot_status"),
        ):
            archived, _, _ = server_archive.run_archive_sync_impl(hot, None, NOW)

        self.assertEqual(archived, 1)
        written = vector_store.client.set_payload.call_args.kwargs["payload"]
        self.assertEqual(written["metadata"][lc.STATUS_FIELD], lc.STATUS_ARCHIVED)
        self.assertTrue(written["metadata"]["archived"])
        self.assertEqual(written["metadata"]["tags"], ["t"])
        self.assertEqual(written["metadata"]["fact"], "f")
        self.assertTrue(
            written["metadata"][lc.PROCESSED_FIELD] & lc.PROCESSED_RETIRED_MASK
        )


class RetentionTest(unittest.TestCase):
    def test_complete_recent_frequently_used_scores_high(self):
        row = _row(
            access=10,
            idle_days=0,
            tags=["t"],
            episode_id="e",
            type="decision",
            context="c",
        )

        self.assertGreaterEqual(retention.quality_score(row, NOW), 0.7)
        self.assertEqual(retention.retention_days(0.9), 365)

    def test_quality_buckets_map_to_the_plan_windows(self):
        self.assertEqual(retention.retention_days(0.7), 365)
        self.assertEqual(retention.retention_days(0.5), 180)
        self.assertGreaterEqual(retention.retention_days(0.25), 30)
        self.assertLessEqual(retention.retention_days(0.25), 90)

    def test_high_quality_entries_wait_out_the_long_window(self):
        row = _row(
            access=10,
            idle_days=100,
            tags=["t"],
            episode_id="e",
            type="decision",
            context="c",
        )

        self.assertFalse(retention.is_expired(row, NOW))

    def test_low_quality_entries_expire_inside_the_short_window(self):
        row = _row(access=0, idle_days=100)

        self.assertTrue(retention.is_expired(row, NOW))

    def test_never_touched_entries_use_created_at(self):
        row = {"created_at": (NOW - timedelta(days=400)).isoformat()}

        self.assertTrue(retention.is_expired(row, NOW))


class ContradictionBandTest(unittest.TestCase):
    def test_only_rows_inside_the_band_survive(self):
        rows = [
            {"id": "near", "score": 0.9, "metadata": {}},
            {"id": "band", "score": 0.6, "metadata": {}},
            {"id": "far", "score": 0.2, "metadata": {}},
            {"id": "self", "score": 0.6, "metadata": {}},
        ]

        band = server_memory._contradiction_band(rows, "self")

        self.assertEqual([row["id"] for row in band], ["band"])

    def test_inactive_rows_are_ignored(self):
        rows = [
            {
                "id": "old",
                "score": 0.6,
                "metadata": {lc.STATUS_FIELD: lc.STATUS_SUPERSEDED},
            }
        ]

        self.assertEqual(server_memory._contradiction_band(rows, "self"), [])

    def test_row_without_a_score_is_ignored(self):
        rows = [{"id": "blank", "metadata": {}}]

        self.assertEqual(server_memory._contradiction_band(rows, "self"), [])


if __name__ == "__main__":
    unittest.main()
