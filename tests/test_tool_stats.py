"""Tool-call statistics: multi-call extraction and tool-output dedupe."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

from agent_gateway.services.proxy import proxy_models, usage_db, usage_record
from agent_gateway.services.proxy.proxy_protocol import (
    _extract_tool_name_from_sse,
    _extract_tool_names_from_sse,
)
from agent_gateway.services.proxy.usage_extract import extract_response_tool_names


class ResponseToolNamesTest(unittest.TestCase):
    def test_anthropic_parallel_calls(self):
        body = {
            "content": [
                {"type": "text", "text": "working"},
                {"type": "tool_use", "name": "Read"},
                {"type": "tool_use", "name": "Grep"},
            ]
        }
        self.assertEqual(extract_response_tool_names(body), ["Read", "Grep"])

    def test_openai_parallel_calls(self):
        body = {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {"function": {"name": "Read"}},
                            {"function": {"name": "Grep"}},
                        ]
                    }
                }
            ]
        }
        self.assertEqual(extract_response_tool_names(body, "openai"), ["Read", "Grep"])

    def test_no_tool_calls(self):
        self.assertEqual(extract_response_tool_names({"content": []}), [])


class SSEToolNamesTest(unittest.TestCase):
    def test_anthropic_collects_every_block(self):
        sse = b"\n".join(
            [
                b'data: {"type":"content_block_start","content_block":'
                b'{"type":"tool_use","name":"Read"}}',
                b'data: {"type":"content_block_start","content_block":'
                b'{"type":"tool_use","name":"Grep"}}',
                b"data: [DONE]",
            ]
        )
        self.assertEqual(_extract_tool_names_from_sse(sse), ["Read", "Grep"])
        self.assertEqual(_extract_tool_name_from_sse(sse), "Read")

    def test_openai_dedupes_per_call_index(self):
        sse = b"\n".join(
            [
                b'data: {"choices":[{"delta":{"tool_calls":['
                b'{"index":0,"function":{"name":"Read"}}]}}]}',
                b'data: {"choices":[{"delta":{"tool_calls":['
                b'{"index":1,"function":{"name":"Read"}}]}}]}',
                b'data: {"choices":[{"delta":{"tool_calls":['
                b'{"index":0,"function":{"arguments":"{}"}}]}}]}',
                b"data: [DONE]",
            ]
        )
        self.assertEqual(_extract_tool_names_from_sse(sse, "openai"), ["Read", "Read"])


class ToolOutputDedupeTest(unittest.TestCase):
    def _patch_paths(self, path: str):
        return (
            mock.patch.object(usage_db, "TOOL_OUTPUT_DB_PATH", path),
            mock.patch.object(usage_record, "TOOL_OUTPUT_DB_PATH", path),
        )

    def test_same_tool_call_id_written_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "tool_output.sqlite")
            patches = self._patch_paths(path)
            for p in patches:
                p.start()
            try:
                usage_db.init_tool_output_db()
                item = {
                    "tool_name": "Read",
                    "tool_call_id": "call_1",
                    "output_text": "x",
                    "output_text_raw": "x",
                    "output_size": 1,
                    "output_lines": 1,
                    "is_error": 0,
                }
                usage_record.record_tool_outputs([item], api_usage_id=1)
                usage_record.record_tool_outputs([item], api_usage_id=2)
                conn = sqlite3.connect(path)
                rows = conn.execute(
                    "SELECT COUNT(*), MIN(api_usage_id) FROM tool_output_log"
                ).fetchone()
                conn.close()
            finally:
                for p in patches:
                    p.stop()

        self.assertEqual(rows, (1, 1))

    def test_init_dedupes_existing_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "tool_output.sqlite")
            conn = sqlite3.connect(path)
            conn.executescript(
                """
                CREATE TABLE tool_output_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    api_usage_id INTEGER,
                    timestamp TEXT NOT NULL,
                    tool_name TEXT,
                    tool_call_id TEXT,
                    output_text TEXT,
                    output_size INTEGER,
                    output_lines INTEGER,
                    is_error INTEGER,
                    stop_reason TEXT,
                    model TEXT,
                    route_tier TEXT
                );
                INSERT INTO tool_output_log
                    (api_usage_id, timestamp, tool_name, tool_call_id)
                VALUES (1, strftime('%Y-%m-%dT%H:%M:%S+00:00','now'), 'Read', 'call_1');
                INSERT INTO tool_output_log
                    (api_usage_id, timestamp, tool_name, tool_call_id)
                VALUES (2, strftime('%Y-%m-%dT%H:%M:%S+00:00','now'), 'Read', 'call_1');
                """
            )
            conn.close()

            patches = self._patch_paths(path)
            for p in patches:
                p.start()
            try:
                usage_db.init_tool_output_db()
                conn = sqlite3.connect(path)
                count = conn.execute("SELECT COUNT(*) FROM tool_output_log").fetchone()[
                    0
                ]
                conn.close()
            finally:
                for p in patches:
                    p.stop()

        self.assertEqual(count, 1)


class UsageToolsEndpointTest(unittest.TestCase):
    def test_counts_every_call_and_legacy_rows_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "token_usage.sqlite")
            with (
                mock.patch.object(usage_db, "DB_PATH", path),
                mock.patch.object(proxy_models, "DB_PATH", path),
            ):
                usage_db.init_db()
                conn = sqlite3.connect(path)
                now = datetime.now(timezone.utc).isoformat()
                conn.execute(
                    "INSERT INTO api_usage "
                    "(timestamp, stop_reason, tool_name, tool_names) "
                    "VALUES (?,?,?,?)",
                    (now, "tool_use", "Read", json.dumps(["Read", "Grep"])),
                )
                conn.execute(
                    "INSERT INTO api_usage "
                    "(timestamp, stop_reason, tool_name, tool_names) "
                    "VALUES (?,?,?,?)",
                    (now, "tool_use", "Grep", None),
                )
                conn.commit()
                conn.close()

                class Request:
                    query = {"days": "1"}

                response = asyncio.run(proxy_models.handle_usage_tools(Request()))
                data = json.loads(response.text)

        counts = {t["name"]: (t["calls"], t["legacy_calls"]) for t in data["tools"]}
        self.assertEqual(counts, {"Read": (1, 0), "Grep": (2, 1)})
        self.assertEqual(data["total_calls"], 3)
        self.assertEqual(data["legacy_calls"], 1)


if __name__ == "__main__":
    unittest.main()
