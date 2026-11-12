import json
import os
import tempfile

os.environ.setdefault("QDRANT_PATH", tempfile.mkdtemp(prefix="agent-gateway-test-"))

import unittest

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from agent_gateway.services.mcp import server

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-11-25",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}

ACCEPT = {"Accept": "application/json, text/event-stream"}


def _payload(response):
    """Read the JSON-RPC body from a plain JSON or an SSE response."""
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        for line in response.text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        raise AssertionError("no SSE data event")
    return response.json()


def _app():
    async def api_ping(request):
        return JSONResponse({"ok": True})

    rest = Starlette(routes=[Route("/api/ping", api_ping, methods=["GET"])])
    return server.build_asgi(rest)


class McpHttpSurfaceTest(unittest.TestCase):
    def test_health_endpoint(self):
        with TestClient(_app(), base_url="http://127.0.0.1:9001") as client:
            response = client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

    def test_api_route_precedes_mcp_route(self):
        with TestClient(_app(), base_url="http://127.0.0.1:9001") as client:
            response = client.get("/api/ping")
        self.assertEqual(response.json(), {"ok": True})

    def test_root_serves_json_rpc_initialize(self):
        with TestClient(_app(), base_url="http://127.0.0.1:9001") as client:
            response = client.post("/", json=INITIALIZE, headers=ACCEPT)
        self.assertEqual(response.status_code, 200)
        body = _payload(response)
        self.assertEqual(body["jsonrpc"], "2.0")
        self.assertIn("result", body)

    def test_mcp_endpoint_alias(self):
        with TestClient(_app(), base_url="http://127.0.0.1:9001") as client:
            response = client.post("/mcp", json=INITIALIZE, headers=ACCEPT)
        self.assertEqual(response.status_code, 200)
        self.assertIn("result", _payload(response))

    def test_oversized_body_is_rejected(self):
        with TestClient(_app(), base_url="http://127.0.0.1:9001") as client:
            response = client.post(
                "/",
                content=b"x" * (5 * 1024 * 1024),
                headers={**ACCEPT, "Content-Type": "application/json"},
            )
        self.assertGreaterEqual(response.status_code, 400)
        self.assertLess(response.status_code, 500)


if __name__ == "__main__":
    unittest.main()
