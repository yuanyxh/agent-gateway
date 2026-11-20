"""Trace ids flow through MCP dispatch, retrieval records and proxy usage."""

import asyncio
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QDRANT_PATH", tempfile.mkdtemp(prefix="agent-gateway-test-"))

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from agent_gateway.core import trace_context
from agent_gateway.services.mcp import server
from agent_gateway.services.proxy import token_proxy, usage_db
from agent_gateway.services.retrieval import trace as retrieval_trace


class TraceContextTest(unittest.TestCase):
    def test_bind_and_reset(self):
        self.assertEqual(trace_context.current(), "")
        token = trace_context.bind("rt_test")
        try:
            self.assertEqual(trace_context.current(), "rt_test")
        finally:
            trace_context.reset(token)
        self.assertEqual(trace_context.current(), "")

    def test_new_id_shape(self):
        trace_id = trace_context.new_id()
        self.assertTrue(trace_id.startswith("rt_"))
        self.assertEqual(len(trace_id.split("_")), 4)

    def test_id_survives_thread_handoff(self):
        token = trace_context.bind("rt_thread")
        try:
            seen = asyncio.run(self._read_in_thread())
        finally:
            trace_context.reset(token)
        self.assertEqual(seen, "rt_thread")

    async def _read_in_thread(self):
        return await asyncio.to_thread(trace_context.current)

    def test_retrieval_record_inherits_current_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            token = trace_context.bind("rt_inherit")
            try:
                with mock.patch.object(retrieval_trace, "trace_dir", return_value=tmp):
                    retrieval_trace.record({"tool": "probe"})
                row = retrieval_trace.recent(1)[-1]
            finally:
                trace_context.reset(token)
        self.assertEqual(row["trace_id"], "rt_inherit")


class DispatchTraceTest(unittest.IsolatedAsyncioTestCase):
    async def test_dispatch_logs_and_clears_trace_id(self):
        seen = {}

        async def probe(_args):
            seen["trace"] = trace_context.current()
            return []

        with mock.patch.dict(server.HANDLERS, {"probe": probe}):
            with self.assertLogs(server.logger, level="INFO") as logs:
                await server._dispatch_tool("probe", {})

        self.assertTrue(seen["trace"].startswith("rt_"))
        self.assertIn(f"trace_id={seen['trace']}", "\n".join(logs.output))
        self.assertEqual(trace_context.current(), "")


class ProxyUsageTraceTest(unittest.TestCase):
    def test_gateway_log_records_trace_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "usage.sqlite")
            with mock.patch.object(usage_db, "DB_PATH", db_path):
                usage_db.init_db()
                token = trace_context.bind("rt_proxy")
                try:
                    usage_db.record_gateway_log(
                        "POST", "/v1/messages", 10, 500, "ProbeError", "boom", 12
                    )
                finally:
                    trace_context.reset(token)
                conn = sqlite3.connect(db_path)
                try:
                    row = conn.execute("SELECT trace_id FROM gateway_log").fetchone()
                finally:
                    conn.close()
        self.assertEqual(row[0], "rt_proxy")


class MiddlewareTraceHeaderTest(unittest.IsolatedAsyncioTestCase):
    async def test_error_response_carries_trace_id(self):
        async def boom(_request):
            raise web.HTTPRequestEntityTooLarge(max_size=1, actual_size=2)

        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "usage.sqlite")
            with mock.patch.object(usage_db, "DB_PATH", db_path):
                usage_db.init_db()
                app = token_proxy.create_app()
                app.router.add_route("GET", "/boom", boom)
                client = TestClient(TestServer(app))
                await client.start_server()
                try:
                    resp = await client.get("/boom")
                finally:
                    await client.close()
                conn = sqlite3.connect(db_path)
                try:
                    row = conn.execute("SELECT trace_id FROM gateway_log").fetchone()
                finally:
                    conn.close()
        self.assertEqual(resp.status, 413)
        self.assertEqual(resp.headers.get("X-Trace-Id"), row[0])


class PipelineTraceTest(unittest.TestCase):
    def test_pipeline_payload_reuses_bound_id(self):
        from types import SimpleNamespace

        from agent_gateway.services.retrieval.pipeline import Pipeline

        query = SimpleNamespace(text="q", semantic_text="q", language="en")
        stub = SimpleNamespace(
            name="notes", fusion=SimpleNamespace(name="f"), reranker=None
        )
        token = trace_context.bind("rt_pipe")
        try:
            payload = Pipeline._trace_payload(
                stub,
                query,
                [],
                SimpleNamespace(status="ok", reason=""),
                [],
                [],
                {},
                "project_notes",
            )
        finally:
            trace_context.reset(token)
        self.assertEqual(payload["trace_id"], "rt_pipe")

    def test_pipeline_payload_falls_back_to_new_id(self):
        from types import SimpleNamespace

        from agent_gateway.services.retrieval.pipeline import Pipeline

        query = SimpleNamespace(text="q", semantic_text="q", language="en")
        stub = SimpleNamespace(
            name="notes", fusion=SimpleNamespace(name="f"), reranker=None
        )
        payload = Pipeline._trace_payload(
            stub,
            query,
            [],
            SimpleNamespace(status="ok", reason=""),
            [],
            [],
            {},
            "project_notes",
        )
        self.assertTrue(payload["trace_id"].startswith("rt_"))
