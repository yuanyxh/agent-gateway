import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QDRANT_PATH", tempfile.mkdtemp(prefix="agent-gateway-test-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_gateway.core import memory_lifecycle as lc  # noqa: E402
from agent_gateway.services.mcp import server_memory  # noqa: E402


class SetMemoryStatusTest(unittest.TestCase):
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
        ):
            result = server_memory.set_memory_status(["m1"], status)

        self.assertEqual(result["updated"], 1)
        self.assertEqual(result["failed"], 0)
        return written

    def test_restore_clears_deleted_at(self):
        written = self._run(
            {
                lc.STATUS_FIELD: lc.STATUS_DELETED,
                lc.DELETED_AT_FIELD: "2026-10-28T10:00:00",
                lc.PROCESSED_FIELD: lc.PROCESSED_CONSUMED_MASK,
            },
            lc.STATUS_ACTIVE,
        )

        self.assertEqual(written[lc.STATUS_FIELD], lc.STATUS_ACTIVE)
        self.assertEqual(written[lc.DELETED_AT_FIELD], "")
        self.assertEqual(written[lc.PROCESSED_FIELD], lc.PROCESSED_SUMMARIZED_MASK)

    def test_restore_clears_supersede_fields(self):
        written = self._run(
            {
                lc.STATUS_FIELD: lc.STATUS_SUPERSEDED,
                lc.SUPERSEDED_BY_FIELD: "m2",
                lc.SUPERSEDED_AT_FIELD: "2026-10-28T10:00:00",
                lc.PROCESSED_FIELD: lc.PROCESSED_CONSUMED_MASK,
            },
            lc.STATUS_ACTIVE,
        )

        self.assertEqual(written[lc.SUPERSEDED_BY_FIELD], "")
        self.assertEqual(written[lc.SUPERSEDED_AT_FIELD], "")
        self.assertEqual(written[lc.PROCESSED_FIELD], lc.PROCESSED_SUMMARIZED_MASK)

    def test_delete_stamps_deleted_at_and_retires(self):
        written = self._run(
            {
                lc.STATUS_FIELD: lc.STATUS_ACTIVE,
                lc.PROCESSED_FIELD: lc.PROCESSED_SUMMARIZED_MASK,
            },
            lc.STATUS_DELETED,
        )

        self.assertEqual(written[lc.STATUS_FIELD], lc.STATUS_DELETED)
        self.assertNotEqual(written[lc.DELETED_AT_FIELD], "")
        self.assertEqual(
            written[lc.PROCESSED_FIELD],
            lc.PROCESSED_SUMMARIZED_MASK | lc.PROCESSED_RETIRED_MASK,
        )


if __name__ == "__main__":
    unittest.main()
