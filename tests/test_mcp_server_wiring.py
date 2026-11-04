import os
import tempfile

os.environ.setdefault("QDRANT_PATH", tempfile.mkdtemp(prefix="agent-gateway-test-"))

import unittest

from mcp.server import MCPServer
from starlette.applications import Starlette
from starlette.testclient import TestClient

from agent_gateway.services.mcp import server
from agent_gateway.services.mcp.server_skills import scan_skills

ACCEPT = {"Accept": "application/json, text/event-stream"}
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


def _client():
    return TestClient(server.build_asgi(Starlette()), base_url="http://127.0.0.1:9001")


def _rpc(client, request_id, method, params=None):
    body = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        body["params"] = params
    return client.post("/", json=body, headers=ACCEPT).json()


def _probe_app():
    mcp = MCPServer("probe")

    async def _boom() -> str:
        raise ValueError("boom")

    mcp.add_tool(_boom, name="__boom__")
    return mcp.streamable_http_app(
        streamable_http_path="/",
        json_response=True,
        stateless_http=True,
        host="127.0.0.1",
    )


class McpServerWiringTest(unittest.TestCase):
    def test_tools_list_contains_static_and_skill_tools(self):
        expected = 14 + len(scan_skills())
        with _client() as client:
            client.post("/", json=INITIALIZE, headers=ACCEPT)
            body = _rpc(client, 2, "tools/list")
        self.assertEqual(len(body["result"]["tools"]), expected)

    def test_unknown_tool_returns_error_result(self):
        with _client() as client:
            client.post("/", json=INITIALIZE, headers=ACCEPT)
            body = _rpc(client, 3, "tools/call", {"name": "missing", "arguments": {}})
        self.assertTrue(body["result"]["isError"])
        self.assertIn("Unknown tool", body["result"]["content"][0]["text"])

    def test_tool_exception_returns_error_result(self):
        with TestClient(_probe_app(), base_url="http://127.0.0.1:9001") as client:
            client.post("/", json=INITIALIZE, headers=ACCEPT)
            body = _rpc(client, 4, "tools/call", {"name": "__boom__", "arguments": {}})
        self.assertTrue(body["result"]["isError"])
        self.assertIn("boom", body["result"]["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
