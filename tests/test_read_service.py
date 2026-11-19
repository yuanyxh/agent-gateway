"""read_url failures keep a concrete error_code in the retrieval trace."""

import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QDRANT_PATH", tempfile.mkdtemp(prefix="agent-gateway-test-"))

from agent_gateway.services.mcp import server
from agent_gateway.services.read import read_service


class ReadFailureTraceTest(unittest.TestCase):
    def test_invalid_url_returns_error_code(self):
        result = read_service.read_url("")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "invalid_url")

    def test_failure_error_code_reaches_trace(self):
        result = read_service.read_url("not a url")
        self.assertEqual(result["error_code"], "invalid_url")
        with mock.patch.object(server.retrieval_trace, "record") as record:
            server._record_web_trace(
                "read_url", "not a url", result, 0.0, status="error"
            )
        self.assertEqual(record.call_args[0][0]["error"], "invalid_url")

    def test_success_trace_has_no_error_key(self):
        payload = {
            "ok": True,
            "id": "d" * 16,
            "url": "https://d.example",
            "title": "D",
        }
        with mock.patch.object(server.retrieval_trace, "record") as record:
            server._record_web_trace(
                "read_url", "https://d.example", payload, 0.0, status="ok"
            )
        self.assertNotIn("error", record.call_args[0][0])

    def test_all_tiers_failed_maps_its_code(self):
        payload = {"ok": False, "error_code": "all_tiers_failed", "attempts": []}
        with mock.patch.object(server.retrieval_trace, "record") as record:
            server._record_web_trace(
                "read_url", "https://x", payload, 0.0, status="error"
            )
        self.assertEqual(record.call_args[0][0]["error"], "all_tiers_failed")


if __name__ == "__main__":
    unittest.main()
