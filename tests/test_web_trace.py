"""Web tools expose result ids and write adoption-linkable traces."""

import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QDRANT_PATH", tempfile.mkdtemp(prefix="agent-gateway-test-"))

from agent_gateway.core.url_identity import url_id
from agent_gateway.services.mcp import server
from agent_gateway.services.search import web_search


class WebSearchIdTest(unittest.TestCase):
    def test_results_carry_url_ids(self):
        rows = [{"url": "https://a.example/x", "title": "A", "snippet": "s"}]
        with mock.patch.object(web_search, "search_report", return_value=(rows, {})):
            payload = web_search.websearch("q", limit=1, fetch_top=0)
        self.assertEqual(payload["results"][0]["id"], url_id("https://a.example/x"))


class WebTraceTest(unittest.TestCase):
    def test_trace_dedupes_pages_and_sets_lead(self):
        first = "a" * 16
        second = "b" * 16
        payload = {
            "results": [{"id": first, "url": "https://a.example", "title": "A"}],
            "pages": [
                {"id": first, "url": "https://a.example", "title": "A"},
                {"id": second, "url": "https://b.example", "title": "B"},
            ],
        }
        with mock.patch.object(server.retrieval_trace, "record") as record:
            trace_id = server._record_web_trace(
                "websearch_engine", "q", payload, 0.0, status="ok"
            )
        row = record.call_args[0][0]
        self.assertEqual(row["trace_id"], trace_id)
        self.assertEqual([item["id"] for item in row["returned"]], [first, second])
        self.assertEqual(row["lead"]["id"], first)
        self.assertGreaterEqual(row["latency_ms"]["total"], 0.0)

    def test_single_read_payload_is_its_own_returned_item(self):
        payload = {"ok": True, "id": "c" * 16, "url": "https://c.example", "title": "C"}
        with mock.patch.object(server.retrieval_trace, "record") as record:
            server._record_web_trace(
                "read_url", "https://c.example", payload, 0.0, status="ok"
            )
        row = record.call_args[0][0]
        self.assertEqual([item["id"] for item in row["returned"]], ["c" * 16])

    def test_empty_payload_has_no_lead(self):
        with mock.patch.object(server.retrieval_trace, "record") as record:
            server._record_web_trace(
                "read_url", "https://x", {"ok": False}, 0.0, status="error"
            )
        self.assertIsNone(record.call_args[0][0]["lead"])


if __name__ == "__main__":
    unittest.main()
