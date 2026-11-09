"""Usage databases drop rows past their retention window on init."""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from agent_gateway.services.proxy import usage_db


class UsageRetentionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="usage-retention-")
        self.usage = os.path.join(self.tmp, "token_usage.sqlite")
        self.tool = os.path.join(self.tmp, "tool_output.sqlite")
        self.patches = [
            mock.patch.object(usage_db, "DB_DIR", self.tmp),
            mock.patch.object(usage_db, "DB_PATH", self.usage),
            mock.patch.object(usage_db, "TOOL_OUTPUT_DB_PATH", self.tool),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_api_usage_drops_rows_older_than_90_days(self):
        usage_db.init_db()
        expired = (datetime.now(timezone.utc) - timedelta(days=120)).isoformat()
        recent = datetime.now(timezone.utc).isoformat()
        conn = sqlite3.connect(self.usage)
        conn.execute("INSERT INTO api_usage(timestamp) VALUES (?)", (expired,))
        conn.execute("INSERT INTO api_usage(timestamp) VALUES (?)", (recent,))
        conn.commit()
        conn.close()

        usage_db.init_db()

        conn = sqlite3.connect(self.usage)
        stamps = [row[0] for row in conn.execute("SELECT timestamp FROM api_usage")]
        conn.close()
        self.assertEqual(stamps, [recent])

    def test_tool_output_drops_rows_older_than_5_days(self):
        usage_db.init_tool_output_db()
        expired = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        recent = datetime.now(timezone.utc).isoformat()
        conn = sqlite3.connect(self.tool)
        conn.execute("INSERT INTO tool_output_log(timestamp) VALUES (?)", (expired,))
        conn.execute("INSERT INTO tool_output_log(timestamp) VALUES (?)", (recent,))
        conn.commit()
        conn.close()

        usage_db.init_tool_output_db()

        conn = sqlite3.connect(self.tool)
        stamps = [
            row[0] for row in conn.execute("SELECT timestamp FROM tool_output_log")
        ]
        conn.close()
        self.assertEqual(stamps, [recent])


if __name__ == "__main__":
    unittest.main()
